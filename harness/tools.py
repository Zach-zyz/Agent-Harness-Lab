"""Student-owned, worktree-rooted tools."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .budget import Budget
from .sandbox import Sandbox


MAX_OBSERVATION_CHARS = 12_000
MAX_FILE_CHARS = 200_000
PROTECTED_PATHS = (
    ".git",
    ".grader_tests",
    "grader_tests",
    "tests",
    "eval",
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
    "pytest",
    "sitecustomize.py",
    "conftest.py",
)
FORBIDDEN_PYTEST_OPTIONS = {
    "-c",
    "-o",
    "-p",
    "--basetemp",
    "--confcutdir",
    "--debug",
    "--ignore",
    "--ignore-glob",
    "--junit-xml",
    "--junitxml",
    "--log-file",
    "--override-ini",
    "--pyargs",
    "--rootdir",
}


class ToolError(RuntimeError):
    pass


class ToolRunner:
    def __init__(self, worktree: str | Path, budget: Budget) -> None:
        self.worktree = Path(worktree).resolve()
        self.budget = budget

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch one validated tool action and return a bounded text observation."""
        if not isinstance(arguments, dict):
            raise ToolError("tool arguments must be a JSON object")

        started_at = time.monotonic()
        is_test = name == "run_tests"
        try:
            if name == "list_files":
                observation = self.list_files(_string_argument(arguments, "path", "."))
            elif name == "read_file":
                observation = self.read_file(
                    _string_argument(arguments, "path"),
                    _integer_argument(arguments, "start_line", 1),
                    _integer_argument(arguments, "end_line", 240),
                )
            elif name == "search":
                observation = self.search(
                    _string_argument(arguments, "query"),
                    _string_argument(arguments, "path", "."),
                )
            elif name == "write_file":
                observation = self.write_file(
                    _string_argument(arguments, "path"),
                    _string_argument(arguments, "content"),
                )
            elif name == "run_tests":
                observation = self.run_tests(
                    _string_argument(arguments, "command", "python -m pytest -q")
                )
            else:
                raise ToolError(f"unknown tool: {name}")
        finally:
            elapsed = time.monotonic() - started_at
            self.budget.charge_tool(name, elapsed, is_test=is_test)
        return _bounded(observation)

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a path inside the worktree and reject traversal or symlink escape."""
        if not isinstance(relative_path, str) or not relative_path:
            raise ToolError("path must be a nonempty string")
        requested = Path(relative_path)
        if requested.is_absolute():
            raise ToolError("absolute paths are not allowed")
        resolved = (self.worktree / requested).resolve()
        if resolved != self.worktree and self.worktree not in resolved.parents:
            raise ToolError(f"path escapes worktree: {relative_path}")
        return resolved

    def list_files(self, relative_path: str = ".") -> str:
        root = self.resolve_path(relative_path)
        if not root.exists():
            raise ToolError(f"path does not exist: {relative_path}")
        if not root.is_dir():
            raise ToolError(f"path is not a directory: {relative_path}")

        entries: list[str] = []
        for path in sorted(root.rglob("*")):
            try:
                relative = path.relative_to(self.worktree)
            except ValueError:
                continue
            if _is_hidden(relative):
                continue
            if path.is_symlink():
                entries.append(f"{relative.as_posix()} -> symlink")
            elif path.is_file():
                entries.append(relative.as_posix())
            elif path.is_dir():
                entries.append(relative.as_posix() + "/")
            if len(entries) >= 400:
                entries.append("... file listing truncated ...")
                break
        return "\n".join(entries) if entries else "(no files)"

    def read_file(self, relative_path: str, start_line: int = 1, end_line: int = 240) -> str:
        if start_line < 1 or end_line < start_line:
            raise ToolError("invalid line range")
        if end_line - start_line > 400:
            raise ToolError("read_file is limited to 401 lines")
        path = self.resolve_path(relative_path)
        self._reject_hidden_path(path)
        if not path.is_file():
            raise ToolError(f"path is not a file: {relative_path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ToolError(f"cannot read text file: {relative_path}") from error
        lines = text.splitlines()
        selected = lines[start_line - 1 : end_line]
        if not selected:
            return "(no lines in requested range)"
        return "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=start_line)
        )

    def search(self, query: str, relative_path: str = ".") -> str:
        if not query:
            raise ToolError("search query must be nonempty")
        root = self.resolve_path(relative_path)
        if not root.exists():
            raise ToolError(f"path does not exist: {relative_path}")
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[str] = []
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.relative_to(self.worktree)
            except ValueError:
                continue
            if _is_hidden(relative):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(f"{relative.as_posix()}:{line_number}: {line}")
                    if len(matches) >= 200:
                        matches.append("... search results truncated ...")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "(no matches)"

    def write_file(self, relative_path: str, content: str) -> str:
        if len(content) > MAX_FILE_CHARS:
            raise ToolError("file content exceeds the write limit")
        path = self.resolve_path(relative_path)
        self._reject_protected_write(path)
        if path.exists() and (path.is_dir() or path.is_symlink()):
            raise ToolError(f"cannot overwrite non-regular file: {relative_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content.encode('utf-8'))} bytes to {relative_path}"

    def run_tests(self, command: str = "pytest -q") -> str:
        arguments = shlex.split(command)
        if not arguments:
            raise ToolError("test command must be nonempty")
        executable = Path(arguments[0]).name
        if executable in {"python", "python3"}:
            if arguments[1:3] != ["-m", "pytest"]:
                raise ToolError("python test commands must invoke 'python -m pytest'")
            arguments = [sys.executable, *arguments[1:]]
        elif executable == "pytest":
            arguments = [sys.executable, "-m", "pytest", *arguments[1:]]
        else:
            raise ToolError("run_tests only accepts pytest commands")
        pytest_arguments = arguments[3:]
        for argument in pytest_arguments:
            if argument.startswith("@"):
                raise ToolError(
                    "pytest response files are not allowed in the agent test tool"
                )
            option = argument.split("=", 1)[0]
            if option in FORBIDDEN_PYTEST_OPTIONS or (
                option.startswith("-") and option[:2] in {"-c", "-o", "-p"}
            ):
                raise ToolError(
                    f"pytest option is not allowed in the agent test tool: {option}"
                )
        for argument in pytest_arguments:
            candidate = argument.split("::", 1)[0]
            if not candidate or candidate.startswith("-"):
                continue
            path = Path(candidate)
            if path.is_absolute() or ".." in path.parts:
                raise ToolError("test paths must remain inside the worktree")
            if _is_hidden(path):
                raise ToolError("grader test paths are not accessible")

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
        environment["HOME"] = str(self.worktree.parent)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(self.worktree)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONHASHSEED"] = "0"
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        # Configuration discovery must stop at the worktree. In the student
        # CLI it lives below the starter's pyproject.toml, which is deliberately
        # unreadable to generated code. These staff options are injected only
        # after rejecting model-supplied configuration/root overrides above.
        arguments = [
            *arguments[:3], "-c", os.devnull,
            f"--rootdir={self.worktree}", f"--confcutdir={self.worktree}",
            *pytest_arguments,
        ]
        try:
            with Sandbox(self.worktree):
                completed = subprocess.run(
                    arguments,
                    cwd=self.worktree,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    env=environment,
                )
        except subprocess.TimeoutExpired as error:
            output = (error.stdout or "") + (error.stderr or "")
            return _bounded(f"exit_code: 124\n{output}\nTest command timed out.")

        return _bounded(
            f"exit_code: {completed.returncode}\n{completed.stdout}{completed.stderr}"
        )

    def _reject_hidden_path(self, path: Path) -> None:
        relative = path.relative_to(self.worktree)
        if _is_hidden(relative):
            raise ToolError(f"hidden path is not accessible: {relative.as_posix()}")

    def _reject_protected_write(self, path: Path) -> None:
        relative = path.relative_to(self.worktree).as_posix()
        for protected in PROTECTED_PATHS:
            if relative == protected or relative.startswith(protected + "/"):
                raise ToolError(f"protected path rejected: {relative}")


def _is_hidden(path: Path) -> bool:
    return any(part in {".git", ".grader_tests", "grader_tests"} for part in path.parts)


def _string_argument(
    arguments: dict[str, Any],
    name: str,
    default: str | None = None,
) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise ToolError(f"argument {name!r} must be a string")
    return value


def _integer_argument(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError(f"argument {name!r} must be an integer")
    return value


def _bounded(text: str) -> str:
    if len(text) <= MAX_OBSERVATION_CHARS:
        return text
    head_size = MAX_OBSERVATION_CHARS // 2
    tail_size = MAX_OBSERVATION_CHARS - head_size
    return (
        text[:head_size]
        + "\n... observation truncated ...\n"
        + text[-tail_size:]
    )
