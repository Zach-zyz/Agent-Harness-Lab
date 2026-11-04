"""Apply a patch to a clean repository and run protected F2P/P2P tests."""

from __future__ import annotations

import ast
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from harness.sandbox import Sandbox, sandboxed


MAX_PATCH_BYTES = 1_000_000
MAX_EVENT_BYTES = 4_000_000
# These tasks repair pure Python algorithms. Keep the local source policy
# explicit for staff/student documentation; changing it requires revalidation.
SOURCE_STDLIB_IMPORTS = frozenset({
    "abc", "base64", "binascii", "bisect", "calendar", "codecs", "collections",
    "copy", "csv", "dataclasses", "datetime", "decimal", "email", "enum",
    "fnmatch", "fractions", "functools", "hashlib", "heapq", "io", "ipaddress",
    "itertools", "json", "math", "numbers", "operator", "os", "pathlib", "re",
    "shlex", "statistics", "string", "time", "typing", "unicodedata", "urllib",
})
SOURCE_RESTRICTED_MEMBERS = {
    "os": frozenset({"path", "name", "sep", "altsep", "extsep", "linesep", "fspath"}),
    "io": frozenset({"StringIO", "BytesIO"}),
    "urllib": frozenset({"parse"}),
}
DEFAULT_PROTECTED_PREFIXES = (
    "tests/",
    "eval/",
    "grader_tests/",
    ".grader_tests/",
    "requirements.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.py",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "pytest.ini",
    ".pytest.ini",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "pytest.py",
    "pytest/",
    "sitecustomize.py",
    "conftest.py",
)


def changed_paths(patch_text: str) -> set[str]:
    """Best-effort header parser for diagnostics; never use this as the security boundary."""
    paths: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[6:]
            if path != "/dev/null":
                paths.add(path)
    return paths


@sandboxed
def initialize_git_baseline(worktree: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "verifier@example.invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Course Verifier"], cwd=worktree, check=True)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)


@sandboxed
def actual_changed_paths(worktree: Path) -> set[str]:
    """Inspect the applied worktree, including untracked files and rename source paths."""
    tracked = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        check=True,
        capture_output=True,
    ).stdout
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in (tracked + untracked).split(b"\0")
        if item
    }


def validate_patch(task_dir: str | Path, patch_path: str | Path) -> dict:
    task_dir = Path(task_dir).resolve()
    patch_path = Path(patch_path).expanduser().absolute()
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    protected = DEFAULT_PROTECTED_PREFIXES + tuple(task.get("protected_prefixes", ()))
    result = {
        "resolved": False,
        "patch_valid": False,
        "f2p_passed": False,
        "p2p_passed": False,
        "protected_paths_clean": False,
        "details": {"header_paths": []},
    }
    try:
        if patch_path.is_symlink() or not patch_path.is_file():
            result["details"]["patch_error"] = "patch must be a regular file"
            return result
        patch_size = patch_path.stat().st_size
    except OSError as error:
        result["details"]["patch_error"] = f"cannot inspect patch: {error}"
        return result
    if patch_size > MAX_PATCH_BYTES:
        result["details"]["patch_error"] = (
            f"patch exceeds {MAX_PATCH_BYTES} byte limit"
        )
        return result
    try:
        patch_text = patch_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        result["details"]["patch_error"] = f"patch is not readable UTF-8 text: {error}"
        return result
    result["details"]["header_paths"] = sorted(changed_paths(patch_text))
    if not patch_text.strip():
        return result

    with tempfile.TemporaryDirectory(prefix="hw2-verify-") as temp:
        worktree = Path(temp) / "repo"
        shutil.copytree(task_dir / "repo", worktree, symlinks=True)
        initialize_git_baseline(worktree)
        with Sandbox(worktree, read_files=(patch_path,)):
            check = subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=worktree,
                text=True,
                capture_output=True,
            )
        if check.returncode:
            result["details"]["patch_error"] = check.stderr
            return result

        # Freeze identities from separate, original staff inputs BEFORE applying
        # any patch. Parametrization may itself import task source at collection.
        timeout = task.get("test_timeout_seconds", 30)
        deadline = time.monotonic() + timeout
        trusted = Path(temp) / "trusted"
        shutil.copytree(task_dir / "repo", trusted, symlinks=True)
        shutil.copytree(task_dir / "grader_tests", trusted / ".grader_tests")
        requested = list(dict.fromkeys(task["fail_to_pass"] + task["pass_to_pass"]))
        inventory = collect_tests(trusted, requested, remaining_seconds(deadline))
        if inventory["returncode"]:
            result["details"]["trusted_collection"] = inventory
            return result
        frozen_ids = tuple(inventory["test_report"]["collected"])
        expected = {
            suite: tuple(
                node_id for node_id in frozen_ids
                if any(_matches_node_id(node_id, _normalize_node_id(selector))
                       for selector in task[field])
            )
            for suite, field in (("f2p", "fail_to_pass"), ("p2p", "pass_to_pass"))
        }
        result["details"]["expected_tests"] = {
            suite: list(node_ids) for suite, node_ids in expected.items()
        }
        with Sandbox(worktree, read_files=(patch_path,)):
            subprocess.run(["git", "apply", str(patch_path)], cwd=worktree, check=True)
        paths = actual_changed_paths(worktree)
        symlink_hits = sorted(path for path in paths if (worktree / path).is_symlink())
        protected_hits = sorted(
            path
            for path in paths
            if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in protected)
        )
        result["details"]["changed_paths"] = sorted(paths)
        result["details"]["protected_hits"] = protected_hits
        result["details"]["symlink_hits"] = symlink_hits
        result["protected_paths_clean"] = not protected_hits and not symlink_hits
        if protected_hits or symlink_hits:
            return result
        shutil.copytree(task_dir / "grader_tests", worktree / ".grader_tests")

        policy_errors = _source_policy_errors(worktree, paths)
        if policy_errors:
            result["details"]["source_policy_errors"] = policy_errors
            f2p = _failed_run("source policy forbids testing/runtime introspection")
            p2p = dict(f2p)
        else:
            f2p = run_tests(
                worktree, task["fail_to_pass"], remaining_seconds(deadline),
                expected_node_ids=expected["f2p"],
            )
            p2p = run_tests(
                worktree, task["pass_to_pass"], remaining_seconds(deadline),
                expected_node_ids=expected["p2p"],
            )
        result["patch_valid"] = True
        result["f2p_passed"] = f2p["returncode"] == 0
        result["p2p_passed"] = p2p["returncode"] == 0
        result["details"]["f2p"] = f2p
        result["details"]["p2p"] = p2p
        result["resolved"] = result["f2p_passed"] and result["p2p_passed"]
        return result


def remaining_seconds(deadline: float) -> float:
    return max(0.01, deadline - time.monotonic())


def _failed_run(message: str) -> dict:
    return {"returncode": 1, "stdout": "", "stderr": "", "report_error": message}


def _normalize_node_id(node_id: str) -> str:
    path, separator, selection = node_id.partition("::")
    return Path(path).as_posix() + separator + selection


def collect_tests(worktree: Path, node_ids: list[str], timeout: float) -> dict:
    """Expand selectors, including parameter instances, from TRUSTED inputs only."""
    result = _invoke_pytest(worktree, node_ids, timeout, mode="collect")
    if "test_report" in result:
        error = _test_report_error(
            result["test_report"], [_normalize_node_id(node) for node in node_ids],
            result["process_returncode"], collect_only=True,
        )
        if error:
            result["returncode"] = result["returncode"] or 1
            result["report_error"] = error
    return result


def run_tests(
    worktree: Path, node_ids: list[str], timeout: float,
    *, expected_node_ids: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """Zero means all independently expected tests passed setup/call/teardown.

    Three-argument staff callers may collect their TRUSTED baseline worktree.
    Grading patched source MUST supply the inventory frozen by validate_patch
    from original staff inputs. Discovering it from patched source is unsafe.
    Anonymous-pipe events are lifecycle evidence, not cryptographic attestation
    against arbitrary same-process Python. See the parent source policy below.
    """
    if not node_ids:
        return _failed_run("no requested tests")
    deadline = time.monotonic() + timeout
    if expected_node_ids is None:
        inventory = collect_tests(worktree, node_ids, remaining_seconds(deadline))
        if inventory["returncode"]:
            return inventory
        expected_node_ids = tuple(inventory["test_report"]["collected"])
    if not expected_node_ids:
        return _failed_run("no independently expected tests")
    result = _invoke_pytest(
        worktree, node_ids, remaining_seconds(deadline), mode="run"
    )
    if "test_report" in result:
        # Parent-authored metadata lets artifact readers reconcile an inventory
        # mismatch with returncode. Grading never trusts a child's expected IDs.
        result["test_report"]["expected_node_ids"] = list(expected_node_ids)
        error = _test_report_error(
            result["test_report"], [_normalize_node_id(node) for node in node_ids],
            result["process_returncode"], expected_node_ids=expected_node_ids,
        )
        if error:
            result["returncode"] = result["returncode"] or 1
            result["report_error"] = error
    return result


@sandboxed
def _invoke_pytest(worktree: Path, node_ids: list[str], timeout: float, *, mode: str) -> dict:
    worktree = Path(worktree).resolve()
    if not node_ids:
        return _failed_run("no requested tests")
    absolute_node_ids = [_absolute_node_id(worktree, node_id) for node_id in node_ids]
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
            "SYSTEMROOT",
            "WINDIR",
            "TMPDIR",
            "TEMP",
            "TMP",
        )
        if key in os.environ
    }
    environment["HOME"] = str(worktree.parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # POSIX local runner: drain all channels concurrently, with bounded buffers
    # and one wall-clock deadline. There is no reopenable report file.
    read_fd, write_fd = os.pipe()
    stdout, stderr, events = bytearray(), bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    failure = None
    process = None
    try:
        process = subprocess.Popen(
            [
                sys.executable, "-I", "-B",
                str(Path(__file__).with_name("_pytest_runner.py").resolve()),
                str(write_fd), mode, str(worktree), *absolute_node_ids,
            ],
            cwd=worktree, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, pass_fds=(write_fd,), env=environment,
            start_new_session=True,
        )
        os.close(write_fd)
        write_fd = -1
        with selectors.DefaultSelector() as channels:
            channels.register(process.stdout, selectors.EVENT_READ, stdout)
            channels.register(process.stderr, selectors.EVENT_READ, stderr)
            channels.register(read_fd, selectors.EVENT_READ, events)
            while channels.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "test run did not complete before timeout"
                    break
                for key, _ in channels.select(min(remaining, 0.05)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        channels.unregister(key.fileobj)
                        continue
                    process.account_output(len(chunk))
                    if process.resource_reason:
                        failure = process.resource_reason
                        break
                    buffer = key.data
                    buffer.extend(chunk)
                    if buffer is events:
                        if len(events) > MAX_EVENT_BYTES:
                            failure = "test event channel exceeded size limit"
                            break
                    else:
                        del buffer[:-4000]
                if failure:
                    break
        if failure:
            process.stop_for_limit(failure)
        process.wait()
        process.check_backend(bytes(stderr))
        if process.resource_reason:
            failure = failure or process.resource_reason
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
        if process is not None:
            process.stdout.close()
            process.stderr.close()
    result = {
        "returncode": process.returncode,
        "process_returncode": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
    if failure:
        result["returncode"] = 124 if "timeout" in failure else 1
        result["report_error"] = failure
        return result
    try:
        result["test_report"] = _report_from_events(bytes(events), mode)
    except (ValueError, UnicodeError) as error:
        result["returncode"] = process.returncode or 1
        result["report_error"] = f"missing or invalid test lifecycle: {error}"
    return result


def _report_from_events(data: bytes, mode: str) -> dict:
    """Reconstruct a report only from a complete ordered event stream and EOF."""
    if not data or not data.endswith(b"\n"):
        raise ValueError("missing or truncated events")
    events = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("invalid event schema")
    if events[0] != {"event": "runner_start", "version": 2, "mode": mode}:
        raise ValueError("missing runner_start")
    report = {
        "version": 1, "completed": False, "collected": [], "tests": [],
        "collection_issues": [], "deselected": [], "session_exitstatus": None,
    }
    stage = "collection"
    for event in events[1:]:
        kind = event.get("event")
        if kind == "runtime_violation":
            raise ValueError(f"testing/runtime references changed: {event.get('references')}")
        elif kind == "collection_issue" and stage == "collection":
            report["collection_issues"].append({
                "nodeid": event.get("nodeid"), "outcome": event.get("outcome"),
            })
        elif kind == "deselected" and stage == "collection":
            if not isinstance(event.get("nodeids"), list):
                raise ValueError("invalid deselection event")
            report["deselected"].extend(event["nodeids"])
        elif kind == "collection_complete" and stage == "collection":
            report["collected"] = event.get("nodeids")
            stage = "tests"
        elif kind == "test_report" and stage == "tests" and mode == "run":
            report["tests"].append({key: value for key, value in event.items() if key != "event"})
        elif kind == "session_finish" and stage in {"collection", "tests"}:
            report["session_exitstatus"] = event.get("exitstatus")
            stage = "finished"
        elif kind == "runner_complete" and stage == "finished":
            report["exitstatus"] = event.get("exitstatus")
            report["completed"] = True
            stage = "complete"
        else:
            raise ValueError(f"unexpected {kind!r} during {stage}")
    if stage != "complete":
        raise ValueError("runner did not complete its lifecycle")
    return report


def _source_policy_errors(worktree: Path, changed: set[str]) -> list[str]:
    """Check ALL repository Python source, including helpers and new modules.

    This deliberately bounded AST policy is NOT a Python sandbox or a complete
    detector of obfuscated/dynamic behavior. Imports must be local Python modules
    or SOURCE_STDLIB_IMPORTS, with SOURCE_RESTRICTED_MEMBERS restrictions. It
    disallows reflection, dynamic code loading, raw file/descriptor access and
    mutation of imported runtime objects, including straightforward aliases.
    Only protected staff test/eval trees are excluded from source inspection.
    Evasion, native code, filesystem/process access and arbitrary same-process
    attacks require external OS isolation; the pipe does not authenticate them.
    """
    forbidden_modules = {
        "pytest", "_pytest", "pluggy", "sys", "inspect", "gc", "ctypes", "types",
        "importlib", "builtins", "marshal", "pickle", "dill", "cloudpickle",
        "__main__", "conftest", "sitecustomize", "usercustomize",
        "__future__",
    }
    forbidden_names = {
        "__import__", "eval", "exec", "compile", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "open", "breakpoint",
    }
    forbidden_attributes = {
        "__subclasses__", "__bases__", "__mro__", "__globals__", "__code__",
        "__closure__", "__builtins__", "__dict__", "__getattribute__",
        "gi_frame", "gi_code", "cr_frame", "f_globals", "f_locals", "f_back", "tb_frame",
        "fdopen", "dup", "dup2", "writev", "readv", "fileno", "FileIO",
        "open", "read_bytes", "write_bytes", "read_text", "write_text",
        "argv", "modules", "_getframe", "settrace", "setprofile", "sys", "os", "builtins",
        "load_module", "exec_module", "import_module", "attrgetter", "methodcaller",
        "get_type_hints", "__loader__", "__spec__",
    }
    excluded = {".git", "tests", "grader_tests", ".grader_tests", "eval"}
    sources = [
        path for path in worktree.rglob("*.py")
        if path.relative_to(worktree).parts[0] not in excluded
        and "__pycache__" not in path.relative_to(worktree).parts
    ]
    local_modules = {
        path.relative_to(worktree).parts[0].removesuffix(".py") for path in sources
    }
    errors = []
    for relative in sorted(changed):
        if Path(relative).suffix in {".pyc", ".pyo", ".so", ".dylib", ".dll", ".pyd"}:
            errors.append(f"{relative}: bytecode/native module changes are prohibited")
    for path in sorted(sources):
        relative = path.relative_to(worktree).as_posix()
        try:
            tree = ast.parse(path.read_bytes(), filename=relative)
        except (OSError, SyntaxError, ValueError) as error:
            errors.append(f"{relative}: cannot inspect Python source: {error}")
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        def imported_name(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id)
            if isinstance(node, ast.Attribute):
                parent = imported_name(node.value)
                return f"{parent}.{node.attr}" if parent else None
            return None

        # Propagate simple module/function aliases independent of source order.
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
        for _ in range(len(assignments) + 1):
            before = dict(aliases)
            for assignment in assignments:
                value = imported_name(assignment.value)
                if value:
                    for target in assignment.targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = value
            if aliases == before:
                break

        def import_error(name):
            root, _, member = name.partition(".")
            if root in forbidden_modules:
                return "testing/runtime introspection import"
            if root not in SOURCE_STDLIB_IMPORTS and root not in local_modules:
                return f"import outside task source policy: {root}"
            allowed = SOURCE_RESTRICTED_MEMBERS.get(root)
            if member and allowed is not None and member.split(".")[0] not in allowed:
                return f"prohibited runtime/descriptor member: {name}"
            return None

        for node in ast.walk(tree):
            reason = None
            if isinstance(node, ast.Import):
                reason = next((error for alias in node.names
                               if (error := import_error(alias.name))), None)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    if node.level or any(
                        alias.name != "annotations" or alias.asname is not None
                        for alias in node.names
                    ):
                        reason = "only 'from __future__ import annotations' is allowed"
                elif not node.level:
                    reason = next((error for alias in node.names if (
                        error := import_error(f"{node.module}.{alias.name}")
                    )), None)
                if any(alias.name in forbidden_attributes for alias in node.names):
                    reason = "runtime introspection/descriptor import"
            elif isinstance(node, ast.Name) and node.id in forbidden_names:
                reason = f"reflective builtin {node.id}"
            elif isinstance(node, ast.Attribute):
                name = imported_name(node)
                if node.attr in forbidden_attributes:
                    reason = f"runtime introspection/descriptor attribute {node.attr}"
                elif name:
                    reason = import_error(name)
                    if isinstance(node.ctx, (ast.Store, ast.Del)):
                        reason = f"mutation of imported runtime object: {name}"
                    elif (node.attr.startswith("_")
                          and name.split(".")[0] in SOURCE_STDLIB_IMPORTS):
                        reason = f"introspection of imported runtime object: {name}"
            if reason:
                errors.append(f"{relative}:{node.lineno}: {reason}")
    return errors


def _matches_node_id(collected: str, requested: str) -> bool:
    """Match file/directory/class selectors and unexpanded parametrized tests."""
    return (
        collected == requested
        or collected.startswith(requested + "::")
        or ("::" not in requested and collected.startswith(requested.rstrip("/") + "/"))
        or ("::" in requested and "[" not in requested and collected.startswith(requested + "["))
        or requested == "."
    )


def _test_report_error(
    report: object, requested: list[str], returncode: int, *,
    expected_node_ids: tuple[str, ...] | list[str] | None = None,
    collect_only: bool = False,
) -> str | None:
    """Validate reports; grading callers MUST supply independently frozen IDs.

    The legacy three-argument form checks artifact consistency using any
    parent-recorded expected_node_ids. That stored claim is not independently
    trusted. run_tests always supplies the original inventory explicitly.
    """
    if not isinstance(report, dict) or report.get("version") != 1:
        return "invalid test report schema"
    if report.get("completed") is not True:
        return "test runner did not complete"
    for field in ("session_exitstatus", "exitstatus"):
        if type(report.get(field)) is not int or report[field] != returncode:
            return f"missing or inconsistent {field}"
    for field in ("collection_issues", "deselected"):
        if report.get(field) != []:
            return f"test run has missing or nonempty {field}"
    collected = report.get("collected")
    if not isinstance(collected, list) or not collected:
        return "test collection is missing or empty"
    if any(not isinstance(node_id, str) or not node_id for node_id in collected):
        return "invalid collected test node id"
    if len(set(collected)) != len(collected):
        return "duplicate collected tests"
    missing = [
        node_id for node_id in requested
        if not any(_matches_node_id(item, node_id) for item in collected)
    ]
    if missing:
        return f"requested tests were not collected: {missing}"
    if any(not any(_matches_node_id(item, node_id) for node_id in requested) for item in collected):
        return "unexpected tests were collected"
    if expected_node_ids is None:
        expected_node_ids = report.get("expected_node_ids")
    if not collect_only and expected_node_ids is not None:
        if (not isinstance(expected_node_ids, (list, tuple)) or not expected_node_ids
                or any(not isinstance(node, str) or not node for node in expected_node_ids)):
            return "missing independently expected tests"
        if len(set(expected_node_ids)) != len(expected_node_ids):
            return "duplicate independently expected tests"
        if set(collected) != set(expected_node_ids):
            missing = sorted(set(expected_node_ids) - set(collected))
            extra = sorted(set(collected) - set(expected_node_ids))
            return f"expected test identities differ: missing={missing}, unexpected={extra}"
    tests = report.get("tests")
    if not isinstance(tests, list):
        return "missing per-test reports"
    if collect_only:
        return None if not tests and returncode == 0 else "invalid trusted collection run"
    phases: dict[str, list[str]] = {node_id: [] for node_id in collected}
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("nodeid"), str):
            return "invalid per-test report"
        node_id = test["nodeid"]
        if node_id not in phases:
            return f"report for uncollected test: {node_id}"
        if test.get("outcome") != "passed" or test.get("wasxfail") is not False:
            return f"test did not pass unconditionally: {node_id} ({test.get('when')})"
        phases[node_id].append(test.get("when"))
    for node_id, outcomes in phases.items():
        if outcomes != ["setup", "call", "teardown"]:
            return f"incomplete or duplicate test phases: {node_id}"
    if returncode != 0:
        return "test runner exited unsuccessfully"
    return None


def _absolute_node_id(worktree: Path, node_id: str) -> str:
    path_text, separator, selection = node_id.partition("::")
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid test node id: {node_id}")
    absolute = str(worktree / path)
    return absolute + (separator + selection if separator else "")
