import json
from pathlib import Path

import pytest

from eval.report import budget_compliant, load_task_record
from eval.run_dev import run_batch


def within_budget():
    return {
        "model_calls": 6, "max_model_calls": 6,
        "total_tokens": 1000, "max_tokens": 120_000,
        "active_seconds": 100.0, "max_active_seconds": 480.0,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_calls", 7), ("total_tokens", 120_001),
        ("active_seconds", 481.0), ("max_model_calls", 13),
        ("max_tokens", 120_001), ("max_active_seconds", 481.0),
        ("active_seconds", float("nan")), ("total_tokens", float("inf")),
        ("model_calls", True), ("max_tokens", 0), ("model_calls", -1),
    ],
)
def test_budget_compliance_rejects_overages_or_invalid_accounting(field, value):
    metrics = within_budget()
    metrics[field] = value
    assert not budget_compliant(metrics)


def test_exact_caps_are_permitted_and_missing_accounting_is_not():
    metrics = within_budget()
    metrics.update(total_tokens=120_000, active_seconds=480.0)
    assert budget_compliant(metrics)
    del metrics["max_tokens"]
    assert not budget_compliant(metrics)


@pytest.mark.parametrize(("returncode", "error"), [(1, ""), (0, "invalid artifacts")])
def test_runner_errors_never_earn_resolution(tmp_path, returncode, error):
    (tmp_path / "metrics.json").write_text(json.dumps(within_budget()))
    (tmp_path / "verification.json").write_text('{"resolved": true}')
    record = load_task_record(
        tmp_path, tmp_path, seed=2026, backend="replay",
        runner_returncode=returncode, error=error,
    )
    assert record["resolved"] is False


def test_resume_rejects_tampered_time_cap_evidence(tmp_path):
    root = Path(__file__).resolve().parents[1]
    options = {
        "task_root": root / "tasks" / "toy",
        "output_root": tmp_path / "runs",
        "backend": "replay", "seed": 2026,
        "config": root / "config" / "default.json",
        "selected": ["parse_bool"], "split": "toy",
    }
    first = run_batch(**options)
    assert first["resolved"] == 1
    episode = options["output_root"] / "parse_bool"
    metrics_path = episode / "metrics.json"
    metrics = json.loads(metrics_path.read_text())
    metrics.update(active_seconds=481.0, termination_reason="budget_exhausted")
    metrics_path.write_text(json.dumps(metrics))
    state_path = episode / "final_state.json"
    state = json.loads(state_path.read_text())
    state["status"] = "budget_exhausted"
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="artifact hashes mismatch"):
        run_batch(**options, resume=True)


@pytest.mark.parametrize("verification", [
    {"resolved": True},
    {"resolved": True, "patch_valid": True, "f2p_passed": True, "p2p_passed": True,
     "protected_paths_clean": True, "details": {}},
])
def test_incomplete_verification_never_certifies_success(tmp_path, verification):
    from eval.run_identity import sha256
    (tmp_path / "patch.diff").write_text("nonempty")
    verification["patch_sha256"] = sha256(tmp_path / "patch.diff")
    (tmp_path / "task.json").write_text(json.dumps({
        "fail_to_pass": ["test_failure.py"], "pass_to_pass": ["test_regression.py"]}))
    (tmp_path / "metrics.json").write_text(json.dumps(within_budget()))
    (tmp_path / "verification.json").write_text(json.dumps(verification))
    record = load_task_record(tmp_path, tmp_path, seed=2026, backend="replay", runner_returncode=0)
    assert record["resolved"] is False


def test_unknown_usage_never_certifies_budget():
    metrics = within_budget()
    metrics["provider"] = {"usage_accounting_complete": False}
    assert not budget_compliant(metrics)
