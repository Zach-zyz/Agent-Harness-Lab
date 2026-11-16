"""Optional localization helpers."""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any


def extract_failure_summary(test_output: str, max_lines: int = 40) -> str:
    """Return a compact failure-oriented summary of test output."""
    lines = test_output.splitlines()
    selected = [
        line
        for line in lines
        if (
            line.startswith(("FAILED ", "ERROR ", "E ", "exit_code:"))
            or "AssertionError" in line
            or "Traceback" in line
        )
    ]
    if not selected:
        selected = lines[-max_lines:]
    return "\n".join(selected[:max_lines])

# ---------------------------------------------------------------------------
# Updated mechanism: localized context pack.
# Rank worktree source files with static signals only (no model call, no code
# execution) and preload the top files, with line numbers, into the first prompt.
# ---------------------------------------------------------------------------

_SKIP_PARTS = {".git", ".grader_tests", "grader_tests", "tests", "__pycache__"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
SCORE_DEFINES_ISSUE_SYMBOL = 5
SCORE_IMPORTED_BY_VISIBLE_TEST = 3
SCORE_FILENAME_IN_ISSUE = 1


def visible_test_paths(worktree: str | Path, command: str) -> list[Path]:
    """Return the test files named by the designated visible command (or all tests/)."""
    root = Path(worktree).resolve()
    try:
        arguments = shlex.split(command)
    except ValueError:
        arguments = []
    paths = []
    for argument in arguments:
        candidate = (root / argument.split("::", 1)[0]).resolve()
        if argument.endswith(".py") or "::" in argument:
            if root in candidate.parents and candidate.is_file():
                paths.append(candidate)
    if not paths:
        paths = sorted((root / "tests").glob("test_*.py")) if (root / "tests").is_dir() else []
    return paths


def _source_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in _SKIP_PARTS for part in relative.parts):
            continue
        if relative.name.startswith("test_") or relative.name == "conftest.py":
            continue
        files.append(path)
    return files


def _imported_modules(test_paths: list[Path]) -> set[str]:
    modules: set[str] = set()
    for path in test_paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
    return modules


def _defined_names(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def rank_source_files(
    worktree: str | Path, issue: str, test_paths: list[Path]
) -> list[dict[str, Any]]:
    """Deterministically score each source file; higher means more likely to need the fix."""
    root = Path(worktree).resolve()
    issue_words = set(_IDENTIFIER.findall(issue))
    modules = _imported_modules(test_paths)
    ranked = []
    for path in _source_files(root):
        relative = path.relative_to(root)
        parts = relative.with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        score = 0
        reasons = []
        hits = sorted(_defined_names(text) & issue_words)
        if hits:
            score += SCORE_DEFINES_ISSUE_SYMBOL * len(hits)
            reasons.append("defines_issue_symbol:" + ",".join(hits))
        if module in modules:
            score += SCORE_IMPORTED_BY_VISIBLE_TEST
            reasons.append("imported_by_visible_test")
        if relative.stem in issue_words:
            score += SCORE_FILENAME_IN_ISSUE
            reasons.append("filename_in_issue")
        ranked.append({
            "path": relative.as_posix(), "score": score,
            "reasons": reasons, "chars": len(text),
        })
    ranked.sort(key=lambda item: (-item["score"], item["path"]))
    return ranked


def build_context_pack(
    worktree: str | Path,
    issue: str,
    test_paths: list[Path],
    *,
    max_files: int = 3,
    max_chars: int = 8000,
) -> tuple[str, dict[str, Any]]:
    """Return (prompt text, trajectory details) for the localized context pack."""
    root = Path(worktree).resolve()
    ranked = rank_source_files(root, issue, test_paths)
    sections = []
    included = []
    used = 0
    for item in ranked:
        if len(included) >= max_files or item["score"] <= 0:
            break
        try:
            text = (root / item["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        numbered = "\n".join(
            f"{number}: {line}" for number, line in enumerate(text.splitlines(), start=1)
        )
        if used + len(numbered) > max_chars:
            continue
        sections.append(
            f"### {item['path']} (score {item['score']}; {', '.join(item['reasons'])})\n"
            f"{numbered or '(empty file)'}"
        )
        included.append(item["path"])
        used += len(numbered)

    included_tests = []
    for test_path in test_paths:
        try:
            relative_test = test_path.resolve().relative_to(root).as_posix()
            text = test_path.read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        numbered = "\n".join(
            f"{number}: {line}" for number, line in enumerate(text.splitlines(), start=1)
        )
        if used + len(numbered) > max_chars:
            continue
        sections.append(
            f"### {relative_test} (visible test; read-only and incomplete, "
            "the issue is the acceptance contract)\n"
            f"{numbered or '(empty file)'}"
        )
        included_tests.append(relative_test)
        used += len(numbered)

    ranking = "\n".join(
        f"- {item['path']}: score {item['score']}" for item in ranked[:10]
    ) or "- (no source files found)"
    body = "\n\n".join(sections) if sections else "(no file matched; inspect the repository)"
    prompt = (
        "Localized context (static ranking by issue symbols and visible-test imports):\n"
        f"{ranking}\n\n{body}\n\n"
        "The files above are already read in full with line numbers. Do not re-read them "
        "unless they change; write the fix directly when the cause is clear."
    )
    details = {
        "ranking": ranked[:10],
        "included": included,
        "context_chars": used,
        "test_paths": [path.name for path in test_paths],
        "included_tests": included_tests,
    }
    return prompt, details