import copy
import difflib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import eval.validate_patch as verifier_module
from eval.validate_patch import (
    MAX_PATCH_BYTES,
    _test_report_error,
    actual_changed_paths,
    changed_paths,
    initialize_git_baseline,
    run_tests,
    validate_patch,
)


ROOT = Path(__file__).resolve().parents[1]


def test_changed_paths_parses_source_diff() -> None:
    patch = "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert changed_paths(patch) == {"pkg/a.py"}


def test_verifier_rejects_test_modification(tmp_path: Path) -> None:
    patch = tmp_path / "bad.diff"
    patch.write_text(
        "--- a/tests/test_visible.py\n"
        "+++ b/tests/test_visible.py\n"
        "@@ -1 +1 @@\n"
        "-assert False\n"
        "+assert True\n",
        encoding="utf-8",
    )
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert not result["resolved"]
    assert not result["protected_paths_clean"]


def test_verifier_requires_regressions(tmp_path: Path) -> None:
    patch = tmp_path / "regression.diff"
    patch.write_text(
        "--- a/boolkit/parser.py\n"
        "+++ b/boolkit/parser.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def parse_bool(value):\n"
        "-    return bool(value)\n"
        "+    if isinstance(value, str) and value.strip().lower() in {'false', 'no', '0'}:\n"
        "+        return False\n"
        "+    raise ValueError('unsupported')\n",
        encoding="utf-8",
    )
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert result["f2p_passed"]
    assert not result["p2p_passed"]
    assert not result["resolved"]


def test_actual_changed_paths_catches_rename_and_new_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_visible.py").write_text("def test_ok():\\n    assert True\\n")
    initialize_git_baseline(repo)
    (repo / "tests" / "test_visible.py").rename(repo / "renamed_test.py")
    (repo / "new_module.py").write_text("VALUE = 1\\n")
    assert actual_changed_paths(repo) == {
        "tests/test_visible.py",
        "renamed_test.py",
        "new_module.py",
    }


def test_verifier_rejects_rename_of_protected_test(tmp_path: Path) -> None:
    task_dir = ROOT / "tasks" / "toy" / "parse_bool"
    worktree = tmp_path / "worktree"
    import shutil
    import subprocess

    shutil.copytree(task_dir / "repo", worktree)
    initialize_git_baseline(worktree)
    (worktree / "tests" / "test_visible.py").rename(worktree / "visible_check.py")
    patch = tmp_path / "rename.diff"
    patch.write_text(
        subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
            cwd=worktree,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
    )
    result = validate_patch(task_dir, patch)
    assert not result["resolved"]
    assert not result["protected_paths_clean"]


def test_verifier_rejects_source_symlink(tmp_path: Path) -> None:
    task_dir = ROOT / "tasks" / "toy" / "parse_bool"
    worktree = tmp_path / "worktree"
    import shutil

    shutil.copytree(task_dir / "repo", worktree)
    initialize_git_baseline(worktree)
    (worktree / "boolkit" / "parser.py").unlink()
    (worktree / "boolkit" / "parser.py").symlink_to("/etc/passwd")
    patch = tmp_path / "symlink.diff"
    patch.write_text(
        subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
            cwd=worktree,
            check=True,
            text=True,
            capture_output=True,
        ).stdout,
        encoding="utf-8",
    )
    result = validate_patch(task_dir, patch)
    assert not result["resolved"]
    assert result["details"]["symlink_hits"] == ["boolkit/parser.py"]


def test_verifier_rejects_oversized_patch(tmp_path: Path) -> None:
    patch = tmp_path / "large.diff"
    patch.write_bytes(b"x" * (MAX_PATCH_BYTES + 1))
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert not result["resolved"]
    assert "exceeds" in result["details"]["patch_error"]


def test_verifier_rejects_non_utf8_patch_without_crashing(tmp_path: Path) -> None:
    patch = tmp_path / "binary.diff"
    patch.write_bytes(b"\xff\xfe\x00\x80")
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert not result["resolved"]
    assert "UTF-8" in result["details"]["patch_error"]


def test_verifier_rejects_symlink_patch_file(tmp_path: Path) -> None:
    target = tmp_path / "target.diff"
    target.write_text(
        "--- a/boolkit/parser.py\n"
        "+++ b/boolkit/parser.py\n"
        "@@ -1 +1 @@\n"
        "-def parse_bool(value): return bool(value)\n"
        "+def parse_bool(value): return value\n",
        encoding="utf-8",
    )
    patch = tmp_path / "linked.diff"
    patch.symlink_to(target)
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert not result["resolved"]
    assert result["details"]["patch_error"] == "patch must be a regular file"


def test_hidden_test_environment_uses_secret_free_allowlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    grader = tmp_path / ".grader_tests"
    grader.mkdir()
    (grader / "test_environment.py").write_text(
        "import os\n"
        "def test_host_secrets_absent():\n"
        "    assert 'COURSE_MODEL_API_KEY' not in os.environ\n"
        "    assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURSE_MODEL_API_KEY", "course-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    result = run_tests(tmp_path, [".grader_tests/test_environment.py"], 10)
    assert result["returncode"] == 0, result


def test_verifier_rejects_pytest_configuration_injection(tmp_path: Path) -> None:
    patch = tmp_path / "collect-only.diff"
    patch.write_text(
        "diff --git a/pytest.ini b/pytest.ini\n"
        "new file mode 100644\n"
        "index 0000000..9faed20\n"
        "--- /dev/null\n"
        "+++ b/pytest.ini\n"
        "@@ -0,0 +1,2 @@\n"
        "+[pytest]\n"
        "+addopts = --collect-only\n",
        encoding="utf-8",
    )
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert not result["resolved"]
    assert not result["protected_paths_clean"]


def test_verifier_rejects_reserved_grader_directory_without_crashing(
    tmp_path: Path,
) -> None:
    patch = tmp_path / "grader-dir.diff"
    patch.write_text(
        "diff --git a/.grader_tests/injected.py b/.grader_tests/injected.py\n"
        "new file mode 100644\n"
        "index 0000000..55bd0ac\n"
        "--- /dev/null\n"
        "+++ b/.grader_tests/injected.py\n"
        "@@ -0,0 +1 @@\n"
        "+raise SystemExit(0)\n",
        encoding="utf-8",
    )
    result = validate_patch(ROOT / "tasks" / "toy" / "parse_bool", patch)
    assert not result["resolved"]
    assert not result["protected_paths_clean"]


def write_bool_patch(tmp_path: Path, source: str) -> Path:
    original = (ROOT / "tasks/toy/parse_bool/repo/boolkit/parser.py").read_text()
    patch = tmp_path / "source.diff"
    patch.write_text(
        "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                source.splitlines(keepends=True),
                fromfile="a/boolkit/parser.py",
                tofile="b/boolkit/parser.py",
            )
        ),
        encoding="utf-8",
    )
    return patch


@pytest.mark.parametrize(
    "source",
    [
        # The release-review exploit: source import exits before collection.
        "import os\nos._exit(0)\n",
        # Exiting during a test must also fail, even after successful collection.
        "import os\ndef parse_bool(value):\n    os._exit(0)\n",
        # pytest itself considers skipped test calls a successful process.
        "import pytest\ndef parse_bool(value):\n    pytest.skip('no assertions')\n",
    ],
    ids=["exit-during-import", "exit-during-call", "skip-during-call"],
)
def test_verifier_rejects_source_that_bypasses_assertions(tmp_path: Path, source: str) -> None:
    result = validate_patch(ROOT / "tasks/toy/parse_bool", write_bool_patch(tmp_path, source))
    assert result["patch_valid"]
    assert result["protected_paths_clean"]
    assert not result["resolved"]
    assert not result["f2p_passed"]
    assert not result["p2p_passed"]
    for suite in ("f2p", "p2p"):
        execution = result["details"][suite]
        assert execution["returncode"] != 0
        assert execution["report_error"]
        if "source_policy_errors" not in result["details"]:
            assert execution["process_returncode"] == 0
    # Verification artifacts must remain JSON serializable, including failures.
    json.dumps(result)


@pytest.mark.parametrize("future", ["", "from __future__ import annotations\n"])
def test_verifier_accepts_genuine_source_fix_with_complete_reports(
    tmp_path: Path, future: str,
) -> None:
    patch = write_bool_patch(
        tmp_path,
        future + "def parse_bool(value):\n"
        "    if isinstance(value, bool):\n"
        "        return value\n"
        "    if isinstance(value, str):\n"
        "        normalized = value.strip().lower()\n"
        "        if normalized in {'true', 'yes', '1'}:\n"
        "            return True\n"
        "        if normalized in {'false', 'no', '0'}:\n"
        "            return False\n"
        "    raise ValueError('unsupported')\n",
    )
    result = validate_patch(ROOT / "tasks/toy/parse_bool", patch)
    assert result["resolved"], result
    for suite, count in (("f2p", 2), ("p2p", 1)):
        execution = result["details"][suite]
        assert execution["returncode"] == 0
        report = execution["test_report"]
        assert len(report["collected"]) == count
        assert len(report["tests"]) == count * 3
        assert {test["outcome"] for test in report["tests"]} == {"passed"}


def write_grader(tmp_path: Path, source: str, name: str = "test_checks.py") -> str:
    grader = tmp_path / ".grader_tests"
    grader.mkdir(exist_ok=True)
    (grader / name).write_text(source, encoding="utf-8")
    return f".grader_tests/{name}"


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\n@pytest.mark.skip(reason='skip')\n"
        "def test_requested():\n    assert False\n",
        "import pytest\ndef test_requested():\n    pytest.xfail('xfail')\n",
        "import pytest\n@pytest.mark.xfail(reason='xfail')\n"
        "def test_requested():\n    assert False\n",
        "import pytest\n@pytest.mark.xfail(reason='xpass')\n"
        "def test_requested():\n    assert True\n",
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def setup():\n    pytest.skip('setup skipped')\n"
        "def test_requested():\n    assert False\n",
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def cleanup():\n    yield\n    pytest.skip('teardown skipped')\n"
        "def test_requested():\n    assert True\n",
    ],
    ids=["skip-mark", "xfail-call", "xfail-mark", "xpass", "setup-skip", "teardown-skip"],
)
def test_run_tests_rejects_nonpasses_despite_pytest_exit_zero(
    tmp_path: Path, source: str
) -> None:
    node_id = write_grader(tmp_path, source)
    result = run_tests(tmp_path, [node_id + "::test_requested"], 10)
    assert result["process_returncode"] == 0, result
    assert result["returncode"] != 0
    assert result["report_error"]


@pytest.mark.parametrize(
    "failure", ["assert False", "pytest.skip('module skipped', allow_module_level=True)"]
)
def test_run_tests_rejects_collection_failure_alongside_passing_tests(
    tmp_path: Path, failure: str
) -> None:
    good = write_grader(tmp_path, "def test_good():\n    assert True\n", "test_good.py")
    bad = write_grader(tmp_path, f"import pytest\n{failure}\n", "test_bad.py")
    result = run_tests(tmp_path, [good, bad], 10)
    assert result["returncode"] != 0
    assert result["test_report"]["collection_issues"]


@pytest.mark.parametrize(
    "cleanup", ["assert False", "pytest.skip('cleanup skipped')", "os._exit(0)"]
)
def test_run_tests_requires_successful_teardown(tmp_path: Path, cleanup: str) -> None:
    node_id = write_grader(
        tmp_path,
        "import os, pytest\n"
        "@pytest.fixture\n"
        f"def resource():\n    yield 42\n    {cleanup}\n"
        "def test_requested(resource):\n    assert resource == 42\n",
    )
    result = run_tests(tmp_path, [node_id], 10)
    assert result["returncode"] != 0, result
    assert result["report_error"]


def test_run_tests_rejects_premature_exit_after_a_passing_test(tmp_path: Path) -> None:
    node_id = write_grader(
        tmp_path,
        "import os\n"
        "def test_first():\n    assert True\n"
        "def test_second():\n    os._exit(0)\n",
    )
    result = run_tests(tmp_path, [node_id], 10)
    assert result["process_returncode"] == 0
    assert result["returncode"] != 0
    assert "missing or invalid test lifecycle" in result["report_error"]


def test_run_tests_does_not_accept_a_fake_stdout_report(
    tmp_path: Path, passing_report: dict
) -> None:
    # Even a fully valid report on stdout is not the staff reporting channel.
    stdout = "100 passed\n" + json.dumps(passing_report)
    # A real isolated child emits forged stdout and truncates its lifecycle.
    # Do not replace Popen: an unexpected wrapper must fail closed.
    write_grader(
        tmp_path,
        "import os\n"
        "def test_first(capfd):\n"
        "    with capfd.disabled():\n"
        f"        os.write(1, {stdout.encode()!r})\n"
        "        os._exit(0)\n"
        "def test_second():\n    assert True\n",
    )
    result = run_tests(
        tmp_path, passing_report["collected"], 10,
        expected_node_ids=passing_report["collected"],
    )
    assert result["process_returncode"] == 0
    assert result["stdout"] == stdout
    assert result["returncode"] != 0
    assert "missing or invalid test lifecycle" in result["report_error"]


def test_annotations_future_directive_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "solution.py").write_text(
        "from __future__ import annotations\n"
        "def f(value: MissingType) -> MissingType:\n    return value\n"
    )
    assert verifier_module._source_policy_errors(tmp_path, {"solution.py"}) == []


@pytest.mark.parametrize("source", [
    "import __future__\n",
    "from __future__ import division\n",
    "from __future__ import annotations, division\n",
    "from __future__ import annotations as feature\n",
    "from __future__ import *\n",
])
def test_future_import_exception_does_not_widen_policy(tmp_path: Path, source: str) -> None:
    (tmp_path / "solution.py").write_text(source)
    assert verifier_module._source_policy_errors(tmp_path, {"solution.py"})


def test_verifier_output_flood_is_bounded(tmp_path: Path) -> None:
    node_id = write_grader(
        tmp_path,
        "import os\n"
        "def test_flood(capfd):\n"
        "    with capfd.disabled():\n"
        "        while True: os.write(1, b'x' * 65536)\n",
    )
    result = run_tests(
        tmp_path, [node_id], 10,
        expected_node_ids=[node_id + "::test_flood"],
    )
    assert result["returncode"] != 0
    assert "output exceeded" in result["report_error"]


def test_run_tests_requires_every_requested_test_to_pass(tmp_path: Path) -> None:
    node_id = write_grader(
        tmp_path,
        "import pytest\n"
        "def test_good():\n    assert True\n"
        "def test_skipped():\n    pytest.skip('requested but not executed')\n",
    )
    requested = [node_id + "::test_good", node_id + "::test_skipped"]
    result = run_tests(tmp_path, requested, 10)
    assert result["process_returncode"] == 0
    assert result["test_report"]["collected"] == requested
    assert result["returncode"] != 0


@pytest.mark.parametrize(
    "requests",
    [[], [".grader_tests/test_checks.py"], [".grader_tests/test_checks.py::test_absent"]],
)
def test_run_tests_rejects_empty_or_missing_requested_tests(
    tmp_path: Path, requests: list[str]
) -> None:
    write_grader(tmp_path, "# No tests collected.\n")
    result = run_tests(tmp_path, requests, 10)
    assert result["returncode"] != 0
    assert result["report_error"]


@pytest.mark.parametrize(
    "selector",
    [
        ".grader_tests/test_checks.py",
        "./.grader_tests/test_checks.py::TestChecks",
        ".grader_tests/test_checks.py::TestChecks::test_value",
        ".grader_tests/test_checks.py::TestChecks::test_value[2]",
        ".grader_tests/",
    ],
)
def test_run_tests_accepts_complete_parameterized_selections(tmp_path: Path, selector: str) -> None:
    write_grader(
        tmp_path,
        "import pytest\n"
        "class TestChecks:\n"
        "    @pytest.mark.parametrize('value', [1, 2])\n"
        "    def test_value(self, value):\n        assert value > 0\n",
    )
    result = run_tests(tmp_path, [selector], 10)
    assert result["returncode"] == 0, result


def test_run_tests_timeout_is_failure_and_json_serializable(tmp_path: Path) -> None:
    node_id = write_grader(
        tmp_path,
        "import os, time\n"
        "def test_wait():\n"
        "    os.write(1, b'started\\n')\n"
        "    time.sleep(30)\n",
    )
    result = run_tests(tmp_path, [node_id], 1)
    assert result["returncode"] == 124
    assert isinstance(result["stdout"], str)
    json.dumps(result)


@pytest.fixture
def passing_report(tmp_path: Path) -> dict:
    node_id = write_grader(
        tmp_path,
        "def test_first():\n    assert True\n"
        "def test_second():\n    assert True\n",
    )
    result = run_tests(tmp_path, [node_id], 10)
    assert result["returncode"] == 0, result
    return result["test_report"]


def test_parent_rejects_missing_explicit_request_despite_other_passes(passing_report: dict) -> None:
    requested = list(passing_report["collected"])
    partial = copy.deepcopy(passing_report)
    partial["collected"].remove(requested[1])
    partial["tests"] = [
        test for test in partial["tests"] if test["nodeid"] != requested[1]
    ]
    assert "requested tests were not collected" in _test_report_error(partial, requested, 0)


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_parent_rejects_missing_phase_with_exit_zero(passing_report: dict, phase: str) -> None:
    partial = copy.deepcopy(passing_report)
    partial["tests"] = [test for test in partial["tests"] if test["when"] != phase]
    assert "incomplete" in _test_report_error(partial, partial["collected"], 0)


def test_parent_rejects_duplicate_reports_with_exit_zero(passing_report: dict) -> None:
    duplicate = copy.deepcopy(passing_report)
    duplicate["tests"].extend(copy.deepcopy(duplicate["tests"]))
    assert "duplicate" in _test_report_error(duplicate, duplicate["collected"], 0)


def test_parent_requires_session_completion(passing_report: dict) -> None:
    del passing_report["session_exitstatus"]
    assert "session_exitstatus" in _test_report_error(
        passing_report, passing_report["collected"], 0
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed", False),
        ("collected", []),
        ("collected", None),
        ("tests", []),
        ("tests", None),
        ("deselected", [".grader_tests/test_checks.py::test_second"]),
    ],
)
def test_parent_rejects_incomplete_reports_even_with_exit_zero(
    passing_report: dict, field: str, value: object
) -> None:
    requested = list(passing_report["collected"])
    passing_report[field] = value
    assert _test_report_error(passing_report, requested, 0) is not None


REPORT_FORGERY_SOURCE = '''import json, os, sys
from pathlib import Path
root, report_path, *selectors = sys.argv[1:]
expected = {
    'test_f2p.py': ['test_all_documented_false_forms', 'test_invalid_values_raise'],
    'test_p2p.py': ['test_true_forms_and_bool_inputs_still_work'],
}
ids = []
for selector in selectors:
    filename, separator, selection = selector.partition('::')
    relative = Path(filename).relative_to(root).as_posix()
    ids.extend(relative + '::' + name for name in expected[Path(filename).name])
report = {
    'version': 1, 'completed': True, 'exitstatus': 0, 'session_exitstatus': 0,
    'collection_issues': [], 'deselected': [], 'collected': ids,
    'tests': [dict(nodeid=node, when=phase, outcome='passed', wasxfail=False)
              for node in ids for phase in ('setup', 'call', 'teardown')],
}
with open(report_path, 'w', encoding='utf-8') as output:
    json.dump(report, output)
os._exit(0)
'''

COLLECTION_SUPPRESSION_SOURCE = '''from _pytest.python import PyCollector
_original_collect = PyCollector.collect
def _filtered_collect(self):
    return [item for item in _original_collect(self)
            if item.name != 'test_invalid_values_raise']
PyCollector.collect = _filtered_collect

def parse_bool(value):
    if isinstance(value, str) and value.strip().lower() in {'false', 'no', '0'}:
        return False
    return bool(value)
'''


@pytest.mark.parametrize(
    "source", [REPORT_FORGERY_SOURCE, COLLECTION_SUPPRESSION_SOURCE],
    ids=["exact-report-forgery", "collection-suppression"],
)
def test_verifier_rejects_security_review_payloads(tmp_path: Path, source: str) -> None:
    result = validate_patch(ROOT / "tasks/toy/parse_bool", write_bool_patch(tmp_path, source))
    assert result["patch_valid"] and result["protected_paths_clean"]
    assert not result["resolved"], result
    assert not result["f2p_passed"], result


def test_parent_requires_frozen_ids_for_whole_file_selection(passing_report: dict) -> None:
    expected = tuple(passing_report["collected"])
    partial = copy.deepcopy(passing_report)
    partial["collected"].pop()
    partial["tests"] = [
        report for report in partial["tests"] if report["nodeid"] != expected[-1]
    ]
    error = _test_report_error(
        partial, [".grader_tests/test_checks.py"], 0, expected_node_ids=expected
    )
    assert error is not None
    assert "expected test" in error


@pytest.mark.parametrize("keep_all_parameters", [False, True])
def test_expected_parameter_ids_are_collected_before_applying_source_patch(
    tmp_path: Path, keep_all_parameters: bool
) -> None:
    task = tmp_path / "task"
    (task / "repo").mkdir(parents=True)
    (task / "grader_tests").mkdir()
    original = "VALUES = [1, 2]\ndef check(value):\n    return value == 1\n"
    (task / "repo/values.py").write_text(original)
    (task / "grader_tests/test_f2p.py").write_text(
        "import pytest\nfrom values import VALUES, check\n"
        "@pytest.mark.parametrize('value', VALUES)\n"
        "def test_check(value):\n    assert check(value)\n"
    )
    (task / "grader_tests/test_p2p.py").write_text(
        "from values import check\n"
        "def test_regression():\n    assert check(1)\n"
    )
    (task / "task.json").write_text(json.dumps({
        "fail_to_pass": [".grader_tests/test_f2p.py"],
        "pass_to_pass": [".grader_tests/test_p2p.py"],
        "test_timeout_seconds": 10,
    }))
    values = "[1, 2]" if keep_all_parameters else "[1]"
    replacement = f"VALUES = {values}\ndef check(value):\n    return True\n"
    patch = tmp_path / "parameters.diff"
    patch.write_text("".join(difflib.unified_diff(
        original.splitlines(True), replacement.splitlines(True),
        fromfile="a/values.py", tofile="b/values.py",
    )))
    result = validate_patch(task, patch)
    assert result["patch_valid"] and result["protected_paths_clean"]
    assert result["resolved"] is keep_all_parameters, result
    assert result["details"]["expected_tests"]["f2p"] == [
        ".grader_tests/test_f2p.py::test_check[1]",
        ".grader_tests/test_f2p.py::test_check[2]",
    ]
    if not keep_all_parameters:
        f2p = result["details"]["f2p"]
        assert f2p["process_returncode"] == 0
        assert "expected test identities differ" in f2p["report_error"]
        assert _test_report_error(
            f2p["test_report"], [".grader_tests/test_f2p.py"], 0
        ) == f2p["report_error"]


@pytest.mark.parametrize(
    "source", [REPORT_FORGERY_SOURCE, COLLECTION_SUPPRESSION_SOURCE],
    ids=["pipe-rejects-file-forgery", "snapshot-rejects-suppression"],
)
def test_transport_and_inventory_reject_probes_independently_of_source_policy(
    tmp_path: Path, monkeypatch, source: str
) -> None:
    # Exercise the other defenses even if the bounded AST policy were bypassed.
    monkeypatch.setattr(verifier_module, "_source_policy_errors", lambda *args: [])
    result = validate_patch(ROOT / "tasks/toy/parse_bool", write_bool_patch(tmp_path, source))
    assert result["patch_valid"] and result["protected_paths_clean"]
    assert not result["resolved"], result
    if source == COLLECTION_SUPPRESSION_SOURCE:
        f2p = result["details"]["f2p"]
        assert f2p["process_returncode"] == 0
        assert "testing/runtime references changed" in f2p["report_error"]


def test_runner_consumes_bootstrap_arguments_before_test_import(tmp_path: Path) -> None:
    node_id = write_grader(
        tmp_path,
        "import sys\n"
        "assert len(sys.argv) == 1, 'reporting arguments exposed to task code'\n"
        "def test_good():\n    assert True\n",
    )
    result = run_tests(tmp_path, [node_id], 10)
    assert result["returncode"] == 0, result


@pytest.mark.parametrize(
    "source",
    [
        "import sys as runtime\n",
        "from _pytest.python import PyCollector as collector\n",
        "loader = __import__\n",
        "frames = object.__subclasses__()\n",
        "from os import fdopen as channel\n",
        "import os as runtime\nalias = runtime\nsend = alias.write\n",
        "import math as library\nalias = library\nalias.sqrt = lambda x: x\n",
    ],
)
def test_parent_policy_rejects_direct_runtime_introspection(tmp_path: Path, source: str) -> None:
    (tmp_path / "solution.py").write_text(source)
    assert verifier_module._source_policy_errors(tmp_path, {"solution.py"})


@pytest.mark.parametrize("truncation", ["before-session-finish", "before-runner-complete"])
def test_parent_requires_lifecycle_completion_after_passing_phases(
    passing_report: dict, truncation: str
) -> None:
    events = [
        {"event": "runner_start", "version": 2, "mode": "run"},
        {"event": "collection_complete", "nodeids": passing_report["collected"]},
        *[{"event": "test_report", **test} for test in passing_report["tests"]],
    ]
    if truncation == "before-runner-complete":
        events.append({"event": "session_finish", "exitstatus": 0})
    encoded = "".join(json.dumps(event) + "\n" for event in events).encode()
    with pytest.raises(ValueError, match="did not complete"):
        verifier_module._report_from_events(encoded, "run")


def test_policy_checks_unchanged_helpers_and_nested_new_modules(tmp_path: Path) -> None:
    (tmp_path / "solution.py").write_text("import helper\n")
    (tmp_path / "helper.py").write_text("import sys\n")
    nested = tmp_path / "package/tests"
    nested.mkdir(parents=True)
    (nested / "new_helper.py").write_text("run = eval\n")
    errors = verifier_module._source_policy_errors(tmp_path, {"solution.py"})
    assert any("helper.py:" in error for error in errors)
    assert any("package/tests/new_helper.py:" in error for error in errors)


def test_snapshot_rejects_skipping_assertions_with_all_expected_ids(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(verifier_module, "_source_policy_errors", lambda *args: [])
    patch = write_bool_patch(
        tmp_path,
        "from _pytest.python import Function\n"
        "Function.runtest = lambda self: None\n"
        "def parse_bool(value):\n    return True\n",
    )
    result = validate_patch(ROOT / "tasks/toy/parse_bool", patch)
    assert result["patch_valid"] and result["protected_paths_clean"]
    assert not result["resolved"]
    for suite in ("f2p", "p2p"):
        execution = result["details"][suite]
        assert execution["process_returncode"] == 0
        assert execution["returncode"] != 0
        assert "testing/runtime references changed" in execution["report_error"]
