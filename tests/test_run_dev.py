from pathlib import Path
import json
import sys

import pytest

from eval import run_dev
from eval.run_dev import (
    _validate_resume_metadata,
    discover_tasks,
    run_batch,
    validate_episode_artifacts,
)
from eval.run_identity import build_run_identity


ROOT = Path(__file__).resolve().parents[1]


def test_public_dev_has_twelve_ordered_tasks() -> None:
    tasks = discover_tasks(ROOT / "tasks" / "dev")
    assert len(tasks) == 12
    assert [task.name for task in tasks] == sorted(task.name for task in tasks)


def test_task_selection_accepts_directory_name_and_task_id() -> None:
    task_root = ROOT / "tasks" / "dev"
    by_name = discover_tasks(task_root, ["casefold_headers"])
    by_id = discover_tasks(task_root, ["dev_casefold_headers"])
    assert by_name == by_id
    assert by_name[0].name == "casefold_headers"


def test_batch_refuses_existing_episode_without_resume(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    (output_root / "casefold_headers").mkdir(parents=True)
    with pytest.raises(ValueError, match="--resume"):
        run_batch(
            task_root=ROOT / "tasks" / "dev",
            output_root=output_root,
            backend="replay",
            seed=2026,
            config=ROOT / "config" / "default.json",
            selected=["casefold_headers"],
        )


def test_resume_rejects_changed_seed_backend_or_config(tmp_path: Path) -> None:
    output = tmp_path / "episode"
    output.mkdir()
    config = tmp_path / "config.json"
    config.write_text('{"max_model_calls": 6}\n', encoding="utf-8")
    (output / "metrics.json").write_text(
        json.dumps(
            {
                "run": build_run_identity(
                    task_dir=ROOT / "tasks" / "toy" / "parse_bool",
                    seed=2026,
                    backend="replay",
                    config=config,
                    model_card=ROOT / "MODEL_CARD.md",
                )
            }
        ),
        encoding="utf-8",
    )
    _validate_resume_metadata(
        output,
        task_dir=ROOT / "tasks" / "toy" / "parse_bool",
        seed=2026,
        backend="replay",
        config=config,
    )
    with pytest.raises(ValueError, match="resume metadata mismatch"):
        _validate_resume_metadata(
            output,
            task_dir=ROOT / "tasks" / "toy" / "parse_bool",
            seed=2027,
            backend="replay",
            config=config,
        )
    config.write_text('{"max_model_calls": 5}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="resume metadata mismatch"):
        _validate_resume_metadata(
            output,
            task_dir=ROOT / "tasks" / "toy" / "parse_bool",
            seed=2026,
            backend="replay",
            config=config,
        )


def test_resume_rejects_changed_task_input(tmp_path: Path) -> None:
    source_task = ROOT / "tasks" / "toy" / "parse_bool"
    task_dir = tmp_path / "task"
    import shutil

    shutil.copytree(source_task, task_dir)
    output = tmp_path / "episode"
    output.mkdir()
    config = ROOT / "config" / "default.json"
    identity = build_run_identity(
        task_dir=task_dir,
        seed=2026,
        backend="replay",
        config=config,
        model_card=ROOT / "MODEL_CARD.md",
    )
    (output / "metrics.json").write_text(
        json.dumps({"run": identity, "termination_reason": "finished"}),
        encoding="utf-8",
    )
    replay = task_dir / "replay.json"
    replay.write_text(replay.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="resume metadata mismatch"):
        _validate_resume_metadata(
            output,
            task_dir=task_dir,
            seed=2026,
            backend="replay",
            config=config,
        )


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("trajectory.jsonl", "{not json}\n", "trajectory.jsonl is invalid"),
        ("metrics.json", "[]\n", "metrics.json must contain a JSON object"),
        ("final_state.json", "{not json}\n", "final_state.json is invalid"),
    ],
)
def test_artifact_validation_rejects_invalid_structured_output(
    tmp_path: Path,
    filename: str,
    content: str,
    expected: str,
) -> None:
    (tmp_path / "provider_metrics.json").write_text("{}")
    (tmp_path / "api_attempts.jsonl").write_text("")
    (tmp_path / "patch.diff").write_text("", encoding="utf-8")
    (tmp_path / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "metrics.json").write_text(
        '{"termination_reason": "finished"}\n',
        encoding="utf-8",
    )
    (tmp_path / "final_state.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / filename).write_text(content, encoding="utf-8")
    assert expected in validate_episode_artifacts(tmp_path)


def test_artifact_validation_treats_internal_error_as_runner_failure(tmp_path: Path) -> None:
    (tmp_path / "provider_metrics.json").write_text("{}")
    (tmp_path / "api_attempts.jsonl").write_text("")
    (tmp_path / "patch.diff").write_text("", encoding="utf-8")
    (tmp_path / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "metrics.json").write_text(
        '{"termination_reason": "error"}\n',
        encoding="utf-8",
    )
    (tmp_path / "final_state.json").write_text("{}\n", encoding="utf-8")
    assert "active_seconds" in validate_episode_artifacts(tmp_path)


def write_valid_episode(output: Path) -> None:
    (output / "provider_metrics.json").write_text('{"usage_accounting_complete": true}')
    (output / "api_attempts.jsonl").write_text("")
    (output / "patch.diff").write_text("patch\n", encoding="utf-8")
    (output / "trajectory.jsonl").write_text('{"kind": "model"}\n', encoding="utf-8")
    (output / "metrics.json").write_text(
        json.dumps(
            {
                "provider": {"usage_accounting_complete": True},
                "token_cap_certified": True,
                "api_latency_and_retry_wait_exempted": False,
                "active_seconds": 1.0,
                "input_tokens": 10,
                "max_active_seconds": 1800.0,
                "max_model_calls": 12,
                "max_tokens": 120000,
                "model_calls": 1,
                "output_tokens": 5,
                "patch_nonempty": True,
                "resolved": True,
                "run": {"identity": "expected"},
                "termination_reason": "finished",
                "test_invocations": 1,
                "test_seconds": 0.1,
                "tool_calls": {"run_tests": 1},
                "total_tokens": 15,
                "visible_tests_passed": True,
            }
        ),
        encoding="utf-8",
    )
    (output / "final_state.json").write_text(
        json.dumps(
            {
                "issue": "fix it",
                "step": 1,
                "status": "finished",
                "summary": "done",
                "last_observation": "tests passed",
                "visible_tests_passed": True,
            }
        ),
        encoding="utf-8",
    )


def test_artifact_validation_requires_metrics_contract_and_identity(
    tmp_path: Path,
) -> None:
    write_valid_episode(tmp_path)
    assert validate_episode_artifacts(
        tmp_path,
        expected_run={"identity": "expected"},
    ) == ""

    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    del metrics["model_calls"]
    (tmp_path / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    assert "model_calls" in validate_episode_artifacts(tmp_path)

    write_valid_episode(tmp_path)
    assert "run identity mismatch" in validate_episode_artifacts(
        tmp_path,
        expected_run={"identity": "different"},
    )


def test_artifact_validation_rejects_empty_trajectory_and_final_state(
    tmp_path: Path,
) -> None:
    write_valid_episode(tmp_path)
    (tmp_path / "trajectory.jsonl").write_text("", encoding="utf-8")
    assert "at least one event" in validate_episode_artifacts(tmp_path)

    write_valid_episode(tmp_path)
    (tmp_path / "final_state.json").write_text("{}\n", encoding="utf-8")
    assert "missing fields" in validate_episode_artifacts(tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_seconds", float("nan")),
        ("test_seconds", float("inf")),
        ("total_tokens", float("-inf")),
    ],
)
def test_artifact_validation_rejects_non_finite_metrics(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    write_valid_episode(tmp_path)
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    metrics[field] = value
    (tmp_path / "metrics.json").write_text(
        json.dumps(metrics),
        encoding="utf-8",
    )
    assert field in validate_episode_artifacts(tmp_path)


def test_run_dev_main_exits_nonzero_on_runner_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_dev,
        "run_batch",
        lambda **kwargs: {"records": [], "runner_failures": 1},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval.run_dev", "--output_root", str(tmp_path / "output")],
    )
    with pytest.raises(SystemExit) as error:
        run_dev.main()
    assert error.value.code == 1


@pytest.mark.parametrize("resource", ["model_calls", "total_tokens", "active_seconds"])
def test_completed_budget_exhaustion_is_retained_as_experiment_evidence(tmp_path, resource):
    write_valid_episode(tmp_path)
    metrics_path = tmp_path / "metrics.json"
    metrics = json.loads(metrics_path.read_text())
    limits = {
        "model_calls": "max_model_calls",
        "total_tokens": "max_tokens",
        "active_seconds": "max_active_seconds",
    }
    metrics[resource] = metrics[limits[resource]] + 1
    if resource == "total_tokens":
        metrics["input_tokens"] = metrics["total_tokens"] - metrics["output_tokens"]
    metrics_path.write_text(json.dumps(metrics))
    assert "exceeds" in validate_episode_artifacts(tmp_path)

    metrics["termination_reason"] = "budget_exhausted"
    metrics["token_cap_certified"] = metrics["total_tokens"] <= metrics["max_tokens"]
    metrics_path.write_text(json.dumps(metrics))
    state_path = tmp_path / "final_state.json"
    state = json.loads(state_path.read_text())
    state["status"] = "budget_exhausted"
    state_path.write_text(json.dumps(state))
    assert validate_episode_artifacts(tmp_path) == ""


@pytest.mark.parametrize("field", ["base_url", "model_id", "action_mode", "reasoning_effort"])
def test_identity_binds_optional_provider_and_requested_settings(tmp_path, monkeypatch, field):
    from types import SimpleNamespace
    from eval import run_identity
    provider = dict(base_url="https://provider.invalid/v1", model_id="requested-alias",
                    action_mode="native", reasoning_effort="low")
    monkeypatch.setattr(run_identity, "resolve_model_config", lambda: SimpleNamespace(**provider))
    options = dict(task_dir=ROOT / "tasks" / "toy" / "parse_bool", seed=2026,
                   backend="endpoint", config=ROOT / "config" / "default.json",
                   model_card=tmp_path / "nonexistent-model-card")
    before = build_run_identity(**options)
    output = tmp_path / "saved"
    output.mkdir()
    (output / "metrics.json").write_text(json.dumps({"run": before}))
    resume_options = {key: value for key, value in options.items() if key != "model_card"}
    _validate_resume_metadata(output, **resume_options)
    provider[field] += "-changed"
    assert build_run_identity(**options) != before
    with pytest.raises(ValueError, match="resume metadata mismatch"):
        _validate_resume_metadata(output, **resume_options)
    assert "api_key" not in json.dumps(before)
    assert "model_contract_sha256" not in before


def test_identity_binds_source_and_runtime(tmp_path, monkeypatch):
    from eval import run_identity
    source = tmp_path / "run_agent.py"
    source.write_text("original = True\n")
    options = dict(task_dir=ROOT / "tasks" / "toy" / "parse_bool", seed=2026,
                   backend="replay", config=ROOT / "config" / "default.json", source_root=tmp_path)
    original = build_run_identity(**options)
    source.write_text("original = False\n")
    assert build_run_identity(**options)["source_sha256"] != original["source_sha256"]
    monkeypatch.setattr(run_identity, "runtime_identity", lambda: {"python": "different"})
    assert build_run_identity(**options)["runtime"] != original["runtime"]


def test_real_replay_resume_after_runtime_patch_preserves_original_metadata(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from eval import run_identity

    options = dict(task_root=ROOT / "tasks/toy", selected=["parse_bool"],
                   output_root=tmp_path / "batch", backend="replay", seed=2026,
                   config=ROOT / "config/default.json")
    first = run_batch(**options)
    assert first["runner_failures"] == 0
    episode = options["output_root"] / "parse_bool"
    before = {name: (episode / name).read_bytes()
              for name in ("metrics.json", "trajectory.jsonl", "final_state.json", "patch.diff")}
    original = first["records"][0]["run"]
    runtime = original["runtime"]
    upgraded = {
        **runtime,
        "python": ".".join(runtime["python"].split(".")[:2]) + ".99 (patch update)",
        "executable_sha256": "f" * 64,
        "platform": runtime["platform"].split("-", 1)[0] + "-updated-release",
    }
    monkeypatch.setattr(run_identity, "runtime_identity", lambda: upgraded)
    monkeypatch.setattr(run_dev, "subprocess", SimpleNamespace(
        run=lambda *args, **kwargs: pytest.fail("resume must not rerun the agent")))
    resumed = run_batch(**options, resume=True)
    assert resumed["runner_failures"] == 0
    assert resumed["records"][0]["run"] == original
    assert all((episode / name).read_bytes() == content for name, content in before.items())
    assert json.loads((episode / "episode_manifest.json").read_text())["run"] == original

    monkeypatch.setattr(run_identity, "runtime_identity", lambda: {**upgraded, "python": "99.1.0"})
    with pytest.raises(ValueError, match="run identity mismatch"):
        run_batch(**options, resume=True)


@pytest.mark.parametrize("field,value", [
    ("executable_sha256", None), ("executable_sha256", []),
    ("python", "3.14.not-a-version"), ("dependencies", {"pytest": []}),
])
def test_resume_rejects_malformed_raw_runtime(tmp_path, field, value):
    options = dict(task_dir=ROOT / "tasks/toy/parse_bool", seed=2026,
                   backend="replay", config=ROOT / "config/default.json")
    identity = build_run_identity(**options)
    if value is None:
        del identity["runtime"][field]
    else:
        identity["runtime"][field] = value
    (tmp_path / "metrics.json").write_text(json.dumps({"run": identity}))
    with pytest.raises(ValueError, match="runtime identity"):
        _validate_resume_metadata(tmp_path, **options)


@pytest.mark.parametrize("relative", ["my_mechanism.py", "my_mechanism/strategy.py"])
def test_extension_source_is_snapshotted_and_invalidates_resume(tmp_path, monkeypatch, relative):
    from eval import run_identity
    (tmp_path / "run_agent.py").write_text("import my_mechanism\n")
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    if source.parent != tmp_path:
        (source.parent / "__init__.py").write_text("")
    source.write_text("strategy = 'original'\n")
    monkeypatch.setattr(run_identity, "ROOT", tmp_path)
    options = dict(task_dir=ROOT / "tasks" / "toy" / "parse_bool", seed=2026,
                   backend="replay", config=ROOT / "config" / "default.json")
    original = build_run_identity(**options)
    assert relative in original["source_files"]
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "metrics.json").write_text(json.dumps({"run": original}))
    run_dev.snapshot_source(output, original, source_root=tmp_path)
    saved_source = output / "sources" / original["source_sha256"] / relative
    assert saved_source.read_text() == source.read_text()
    _validate_resume_metadata(output, **options)
    source.write_text("strategy = 'changed'\n")
    assert build_run_identity(**options)["source_sha256"] != original["source_sha256"]
    with pytest.raises(ValueError, match="resume metadata mismatch"):
        _validate_resume_metadata(output, **options)
    assert saved_source.read_text() == "strategy = 'original'\n"


@pytest.mark.parametrize("failure_origin", ["runner", "verifier"])
def test_resume_retains_failed_outcome_and_hashes_every_artifact(tmp_path, monkeypatch, failure_origin):
    from types import SimpleNamespace
    calls = []
    options = dict(task_root=ROOT / "tasks" / "toy", selected=["parse_bool"],
                   output_root=tmp_path / "batch", backend="replay", seed=2026,
                   config=ROOT / "config" / "default.json")

    def offline_failure(command, **kwargs):
        calls.append(command)
        episode = Path(command[command.index("--output_dir") + 1])
        episode.mkdir()
        write_valid_episode(episode)
        metrics = json.loads((episode / "metrics.json").read_text())
        metrics["run"] = build_run_identity(
            task_dir=options["task_root"] / "parse_bool", seed=2026,
            backend="replay", config=options["config"])
        metrics["termination_reason"] = "error" if failure_origin == "runner" else "finished"
        (episode / "metrics.json").write_text(json.dumps(metrics))
        state = json.loads((episode / "final_state.json").read_text())
        state["status"] = metrics["termination_reason"]
        (episode / "final_state.json").write_text(json.dumps(state))
        (episode / "extra-diagnostic.txt").write_text("retained failure")
        return SimpleNamespace(returncode=1 if failure_origin == "runner" else 0, stdout="",
                               stderr="offline transport failure" if failure_origin == "runner" else "")

    monkeypatch.setattr(run_dev, "subprocess", SimpleNamespace(run=offline_failure))
    verifications = []

    def verify(*args):
        verifications.append(args)
        if failure_origin == "verifier" and len(verifications) == 1:
            raise RuntimeError("offline verifier unavailable")
        return {"resolved": False, "patch_valid": False, "f2p_passed": False,
                "p2p_passed": False, "protected_paths_clean": False, "details": {}}

    monkeypatch.setattr(run_dev, "validate_patch", verify)
    first = run_batch(**options)
    second = run_batch(**options, resume=True)
    assert len(calls) == 1
    assert first["records"][0]["error"] == second["records"][0]["error"]
    assert first["runner_failures"] == second["runner_failures"] == 1
    assert second["records"][0]["outcome"] == "error"
    assert len(list((options["output_root"] / "parse_bool" / "verification_history").glob("*.json"))) == 1
    diagnostic = options["output_root"] / "parse_bool" / "extra-diagnostic.txt"
    diagnostic.write_text("changed after failure")
    with pytest.raises(ValueError, match="artifact hashes mismatch"):
        run_batch(**options, resume=True)
