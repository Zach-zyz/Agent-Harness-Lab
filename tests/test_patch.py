import subprocess
from pathlib import Path

from harness.patch import git_diff
from harness.sandbox import Sandbox


def test_git_diff_includes_new_source_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (tmp_path / "new_module.py").write_text("NEW_VALUE = 2\n")
    patch = git_diff(tmp_path)
    assert "new_module.py" in patch
    assert "NEW_VALUE = 2" in patch


def test_git_hooks_and_fsmonitor_are_disabled(tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "hooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\necho unsafe > '{marker}'\nexit 1\n")
    hook.chmod(0o700)
    with Sandbox(tmp_path):
        for args in (
            ["init", "-q"], ["config", "user.email", "test@example.invalid"],
            ["config", "user.name", "Test"],
            ["config", "core.hooksPath", str(hook.parent)],
            ["config", "core.fsmonitor", str(hook)],
            ["add", "."], ["commit", "-qm", "base"],
        ):
            subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "solution.py").write_text("VALUE = 42\n")
    patch = git_diff(tmp_path)
    assert "VALUE = 42" in patch
    assert not marker.exists()
