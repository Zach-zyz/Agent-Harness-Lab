import csv
import json
from pathlib import Path

import pytest

from eval.report import write_batch_reports, write_json, write_paired_reports


def _record(task_id: str, output_dir: Path, resolved: bool, seed: int = 2026) -> dict:
    output_dir.mkdir(parents=True)
    return {
        "task_id": task_id,
        "task_name": task_id,
        "seed": seed,
        "backend": "replay",
        "runner_returncode": 0,
        "resolved": resolved,
        "patch_valid": True,
        "f2p_passed": resolved,
        "p2p_passed": True,
        "visible_tests_passed": resolved,
        "model_calls": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "active_seconds": 1.5,
        "test_invocations": 1,
        "test_seconds": 0.2,
        "termination_reason": "finished",
        "output_dir": str(output_dir),
        "error": "",
    }


def test_write_json_rejects_non_finite_numbers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        write_json(tmp_path / "invalid.json", {"value": float("nan")})


def test_batch_reports_write_json_csv_and_failure_index(tmp_path: Path) -> None:
    records = [
        _record("task_a", tmp_path / "task_a", True),
        _record("task_b", tmp_path / "task_b", False),
    ]
    summary = write_batch_reports(records, tmp_path, split="test")
    assert summary["resolved"] == 1
    assert summary["tasks"] == 2
    assert summary["runner_failures"] == 0
    assert json.loads((tmp_path / "summary.json").read_text())["resolve_rate"] == 0.5
    with (tmp_path / "tasks.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["task_id"] for row in rows] == ["task_a", "task_b"]
    failure_index = (tmp_path / "failure_index.md").read_text()
    assert "`task_b`" in failure_index
    assert "task_b/trajectory.jsonl" in failure_index


def test_batch_reports_count_runner_and_artifact_failures(tmp_path: Path) -> None:
    output_root = tmp_path / "failures"
    runner_failure = _record("runner", output_root / "runner", False)
    runner_failure["runner_returncode"] = 2
    artifact_failure = _record("artifact", output_root / "artifact", False)
    artifact_failure["error"] = "run_agent did not produce patch.diff"
    summary = write_batch_reports(
        [runner_failure, artifact_failure],
        output_root,
        split="test",
    )
    assert summary["runner_failures"] == 2


def test_paired_reports_require_same_order_and_seed(tmp_path: Path) -> None:
    baseline = [
        _record("task_a", tmp_path / "a1", False),
        _record("task_b", tmp_path / "b1", True),
    ]
    candidate = [
        _record("task_a", tmp_path / "a2", True),
        _record("task_b", tmp_path / "b2", True),
    ]
    summary = write_paired_reports(
        {"baseline": baseline, "candidate": candidate},
        tmp_path / "paired",
    )
    assert summary["comparisons_to_first_condition"]["candidate"] == {
        "baseline": "baseline",
        "wins": 1,
        "losses": 0,
        "ties": 1,
        "resolve_rate_delta": 0.5,
    }

    with pytest.raises(ValueError, match="task order"):
        write_paired_reports(
            {"baseline": baseline, "candidate": list(reversed(candidate))},
            tmp_path / "bad",
        )


@pytest.mark.parametrize("field", ["model_id", "base_url", "action_mode", "reasoning_effort", "sampling", "runtime"])
def test_paired_rejects_changed_provider_settings(tmp_path, field):
    from eval.run_identity import runtime_identity

    baseline = _record("task", tmp_path / "base", False)
    candidate = _record("task", tmp_path / "candidate", False)
    baseline["run"] = {"model_id": "optional", "base_url": "https://api.example/v1",
                       "action_mode": "native", "reasoning_effort": "low",
                       "sampling": {"temperature": 0.2}, "runtime": runtime_identity()}
    candidate["run"] = {**baseline["run"], field: "changed"}
    with pytest.raises(ValueError, match="model/provider/settings/runtime"):
        write_paired_reports({"full": [baseline], "ablation": [candidate]}, tmp_path / "paired")


def test_paired_unequal_budget_requires_declaration(tmp_path):
    baseline = _record("task", tmp_path / "base", False)
    candidate = _record("task", tmp_path / "candidate", False)
    baseline["budget"] = {"max_model_calls": 12}
    candidate["budget"] = {"max_model_calls": 6}
    with pytest.raises(ValueError, match="budget-variable"):
        write_paired_reports({"full": [baseline], "ablation": [candidate]}, tmp_path / "paired")
    result = write_paired_reports({"full": [baseline], "ablation": [candidate]},
                                  tmp_path / "paired", budget_variable=True)
    assert result["budget_variable"] is True
