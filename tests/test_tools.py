from pathlib import Path
import os
import json
import subprocess
import sys
import tempfile

import pytest

from harness.budget import Budget
from harness.tools import ToolError, ToolRunner


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    runner = ToolRunner(tmp_path, Budget())
    with pytest.raises(ToolError):
        runner.resolve_path("../secret.txt")


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    (worktree / "escape").symlink_to(outside, target_is_directory=True)
    runner = ToolRunner(worktree, Budget())
    with pytest.raises(ToolError):
        runner.resolve_path("escape/secret.txt")


def test_protected_write_is_rejected(tmp_path: Path) -> None:
    runner = ToolRunner(tmp_path, Budget())
    with pytest.raises(ToolError):
        runner.write_file("tests/test_visible.py", "assert True\n")


def test_write_read_and_search(tmp_path: Path) -> None:
    runner = ToolRunner(tmp_path, Budget())
    runner.write_file("pkg/example.py", "alpha = 1\nbeta = 2\n")
    assert "alpha = 1" in runner.read_file("pkg/example.py")
    assert "pkg/example.py" in runner.search("beta")


def test_test_subprocess_does_not_inherit_endpoint_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test_secret.py").write_text(
        "import os\n"
        "def test_secret_absent():\n"
        "    assert 'COURSE_MODEL_API_KEY' not in os.environ\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURSE_MODEL_API_KEY", "secret")
    runner = ToolRunner(tmp_path, Budget())
    assert runner.run_tests("python -m pytest -q").startswith("exit_code: 0")


def test_test_subprocess_does_not_inherit_arbitrary_host_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test_secret.py").write_text(
        "import os\n"
        "def test_host_credentials_absent():\n"
        "    assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    runner = ToolRunner(tmp_path, Budget())
    assert runner.run_tests("python -m pytest -q").startswith("exit_code: 0")


def test_test_subprocess_ignores_inherited_pytest_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n"
        "    assert False\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "missing_untrusted_plugin")
    runner = ToolRunner(tmp_path, Budget())
    assert runner.run_tests("python -m pytest -q").startswith("exit_code: 1")


def test_test_subprocess_rejects_output_path_options(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped-junit.xml"
    runner = ToolRunner(tmp_path, Budget())
    with pytest.raises(ToolError, match="pytest option is not allowed"):
        runner.run_tests(f"python -m pytest -q --junitxml={outside}")
    assert not outside.exists()


def test_test_subprocess_rejects_pytest_response_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped-junit.xml"
    (tmp_path / "pytest-args.txt").write_text(
        f"--junitxml={outside}\n",
        encoding="utf-8",
    )
    runner = ToolRunner(tmp_path, Budget())
    with pytest.raises(ToolError, match="response files"):
        runner.run_tests("python -m pytest -q @pytest-args.txt")
    assert not outside.exists()


def test_test_subprocess_rejects_debug_output_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped-debug.log"
    runner = ToolRunner(tmp_path, Budget())
    with pytest.raises(ToolError, match="pytest option is not allowed"):
        runner.run_tests(f"python -m pytest -q --debug={outside}")
    assert not outside.exists()


def test_generated_source_cannot_read_outside_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    secret = tmp_path / "host-canary"
    secret.write_text("disposable secret")
    (worktree / "solution.py").write_text(
        f"from pathlib import Path\nVALUE = Path({str(secret)!r}).read_text()\n"
    )
    (worktree / "test_solution.py").write_text(
        "import solution\n"
        "def test_solution():\n    assert solution.VALUE\n"
    )
    observation = ToolRunner(worktree, Budget()).run_tests("python -m pytest -q")
    assert not observation.startswith("exit_code: 0"), observation
    assert "PermissionError" in observation or "FileNotFoundError" in observation
    assert "disposable secret" not in observation


def test_real_replay_cli_below_starter_ignores_ancestor_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "pyproject.toml").is_file()
    # A generic /tmp fixture cannot reproduce pytest's upward discovery of the
    # real starter config. Exercise both complete replay/verification workflows.
    with tempfile.TemporaryDirectory(prefix=".test-replay-", dir=root) as temp:
        output = Path(temp) / "trial"
        environment = {
            key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
            if key in os.environ
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "eval.run_toy", "--output_root", str(output)],
            cwd=root, capture_output=True, text=True, timeout=120, env=environment,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        verification = json.loads((output / "verification.json").read_text())
        assert set(verification) == {"clamp_zero", "parse_bool"}
        assert all(result["resolved"] for result in verification.values())
        for task in verification:
            metrics = json.loads((output / task / "metrics.json").read_text())
            assert metrics["visible_tests_passed"] is True
