from pathlib import Path

import pytest

from run_agent import (
    config_sha256,
    load_config,
    prepare_output_dir,
    redact,
    validate_directories,
)


def test_prepare_output_dir_refuses_unmarked_directory(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("user data\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        prepare_output_dir(output)
    assert (output / "keep.txt").is_file()


def test_prepare_output_dir_refuses_marked_directory_with_artifacts(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / ".cs8803_hw2_output").write_text("managed\n", encoding="utf-8")
    (output / "patch.diff").write_text("important\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        prepare_output_dir(output)
    assert (output / "patch.diff").read_text(encoding="utf-8") == "important\n"


def test_validate_directories_rejects_output_inside_repo(tmp_path: Path) -> None:
    task = tmp_path / "task"
    (task / "repo").mkdir(parents=True)
    (task / "task.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        validate_directories(task, task / "repo" / "output")


def test_load_config_rejects_soft_budget_above_hard_cap(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"max_model_calls": 13}\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(path)


@pytest.mark.parametrize("seconds, accepted", [(1800, True), (1801, False)])
def test_load_config_enforces_thirty_minute_cap(tmp_path, seconds, accepted):
    path = tmp_path / "config.json"
    path.write_text('{"max_active_seconds": %d}\n' % seconds)
    if accepted:
        assert load_config(path)["max_active_seconds"] == seconds
    else:
        with pytest.raises(SystemExit, match="max_active_seconds"):
            load_config(path)


def test_load_config_rejects_non_boolean_strong_profile_options(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"bootstrap_context": "yes"}\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(path)


def test_redact_recurses_through_artifact_payload() -> None:
    payload = {"message": "key=secret", "nested": ["secret", {"value": "ok"}]}
    assert redact(payload, ("secret",)) == {
        "message": "key=[REDACTED]",
        "nested": ["[REDACTED]", {"value": "ok"}],
    }


def test_config_sha256_changes_with_exact_config_bytes(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"max_model_calls": 6}\n', encoding="utf-8")
    first = config_sha256(path)
    path.write_text('{"max_model_calls": 6}\n\n', encoding="utf-8")
    assert config_sha256(path) != first


@pytest.mark.parametrize("error", [RuntimeError("patch export fixture"), KeyboardInterrupt()])
def test_patch_export_failure_retains_state_and_recoverable_worktree(tmp_path, monkeypatch, error):
    import json
    import sys
    import run_agent
    task = Path(run_agent.__file__).parent / "tasks/toy/clamp_zero"
    output = tmp_path / "trial"
    monkeypatch.setattr(sys, "argv", [
        "run_agent", "--task_dir", str(task), "--output_dir", str(output),
        "--seed", "2026", "--backend", "replay",
    ])
    def fail_export(worktree):
        raise error
    monkeypatch.setattr(run_agent, "git_diff", fail_export)
    with pytest.raises(SystemExit) as caught:
        run_agent.main()
    assert caught.value.code == (130 if isinstance(error, KeyboardInterrupt) else 1)
    assert (output / "trajectory.jsonl").is_file()
    assert (output / "metrics.json").is_file()
    state = json.loads((output / "final_state.json").read_text())
    assert state["step"] > 0
    recovery = json.loads((output / "RECOVERY.json").read_text())
    assert Path(recovery["worktree"]).is_dir()
    events = [json.loads(line) for line in (output / "trajectory.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "model" for event in events)
    from tools.submission import _check_trajectory
    _check_trajectory(
        output / "trajectory.jsonl",
        json.loads((output / "metrics.json").read_text()),
        state,
    )


@pytest.mark.parametrize("config", [
    '{"max_active_seconds": NaN}', '{"max_model_calls": 1.5}', '{"max_total_tokens": 2.5}',
])
def test_config_rejects_nonfinite_and_fractional_resource_caps(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(config)
    with pytest.raises(SystemExit):
        load_config(path)
