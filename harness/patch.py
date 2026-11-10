"""Provided patch construction helper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from .sandbox import sandboxed


@sandboxed
def git_diff(worktree: str | Path) -> str:
    # Intent-to-add makes new source files appear in the patch without staging their contents.
    subprocess.run(
        ["git", "add", "--intent-to-add", "--", "."],
        cwd=worktree,
        check=True,
        text=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"],
        cwd=worktree,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout
