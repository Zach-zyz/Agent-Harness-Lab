import ast
import copy
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import run_agent
from eval.report import load_task_record, write_batch_reports, write_json, write_paired_reports
from eval.run_dev import discover_tasks, write_episode_manifest, snapshot_source, episode_hashes
from eval.run_identity import sha256, task_input_sha256, build_run_identity, source_files
from eval.validate_patch import _test_report_error, validate_patch
from harness.agent import RepairAgent
from harness.budget import Budget
from harness.model_client import ModelResponse
from harness.tools import ToolRunner
from tools import submission
from tools.submission import SubmissionError, check_submission, package_submission


ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "casefold_headers"
EPISODE = f"baseline/{TASK_NAME}"
PAIRED_EPISODE = f"paired/conditions/a_greedy/{TASK_NAME}"
EPISODE_FILES = (
    "metrics.json", "trajectory.jsonl", "patch.diff", "final_state.json", "verification.json",
    "provider_metrics.json", "api_attempts.jsonl", "episode_manifest.json",
)
DELETE = object()



@pytest.fixture(autouse=True)
def release_contract(tmp_path, monkeypatch):
    # Parent regenerates the release hashes after concurrent runtime edits. Tests
    # use a current local trust anchor; production always reads the release file.
    manifest = tmp_path / "trusted-integrity.json"
    files = source_files(ROOT)
    protected = {name: digest for name, digest in files.items()
                 if name.startswith("eval/") or name in {
                     "harness/model_client.py", "harness/budget.py", "harness/sandbox.py",
                     "requirements.lock", "pyproject.toml"}}
    protected["config/examples/six_calls.json"] = sha256(ROOT / "config/examples/six_calls.json")
    write_json(manifest, {"sha256": protected, "naive_sha256": files})
    monkeypatch.setattr(submission, "INTEGRITY_MANIFEST", manifest)
    monkeypatch.setenv("HW2_MODEL_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("HW2_MODEL_ID", "optional-model")
    monkeypatch.setenv("HW2_MODEL_API_KEY", "local-development-only")
    monkeypatch.setenv("HW2_MODEL_ACTION_MODE", "json")
    monkeypatch.setenv("HW2_MODEL_REASONING_EFFORT", "")


def _bind_episode(episode):
    metrics = json.loads((episode / "metrics.json").read_text())
    verification_path = episode / "verification.json"
    if verification_path.exists():
        verification = json.loads(verification_path.read_text())
        verification.update(patch_sha256=sha256(episode / "patch.diff"),
                            task_input_sha256=metrics["run"]["task_input_sha256"])
        write_json(verification_path, verification)
    failure = metrics["termination_reason"] in {"error", "interrupted"}
    write_episode_manifest(episode, metrics["run"], 1 if failure else 0,
                           "run_agent terminated with error" if failure else "")


def _provider_fixture(episode):
    metrics = json.loads((episode / "metrics.json").read_text())
    events = [json.loads(line) for line in (episode / "trajectory.jsonl").read_text().splitlines()]
    models = [event["payload"] for event in events if event["kind"] == "model"]
    requests = [event["payload"] for event in events if event["kind"] == "model_request"]
    run = metrics["run"]
    stats = {key: 0 for key in (
        "logical_calls", "http_attempts", "retries", "http_429", "http_5xx",
        "request_seconds", "pacing_wait_seconds", "provider_wait_seconds", "provider_requested_wait_seconds",
        "unknown_usage_attempts", "reserved_unknown_tokens", "reported_total_tokens", "reported_reasoning_tokens",
        "reasoning_usage_reported_attempts")}
    stats.update(logical_calls=len(requests), http_attempts=len(models), usage_accounting_complete=True,
                 returned_models=["provider-returned-alias"] if models else [])
    from harness.model_client import account_usage
    attempts = []
    for index, response in enumerate(models, 1):
        usage = {"prompt_tokens": response["input_tokens"], "completion_tokens": response["output_tokens"],
                 "total_tokens": response["input_tokens"] + response["output_tokens"]}
        stats["reported_total_tokens"] += usage["total_tokens"]
        event = {"attempt": index, "logical_call": index, "requested_model": run["model_id"],
                 "base_url": run["base_url"], "action_mode": run["action_mode"],
                 "generation": {**run["sampling"], "max_tokens": run["sampling"]["max_output_tokens"],
                                "reasoning_effort": run["reasoning_effort"]},
                 "request": {"model": run["model_id"], "messages": [{"role": "user", "content": "offline fixture"}],
                             "temperature": run["sampling"]["temperature"], "top_p": run["sampling"]["top_p"],
                             "max_tokens": run["sampling"]["max_output_tokens"]}}
        attempts.append({**event, "phase": "request_started"})
        attempts.append({**event, "phase": "response_received", "http_status": 200, "request_seconds": 0.0,
                         "usage_status": "reported", "usage_raw": usage, **account_usage(usage),
                         "returned_model": "provider-returned-alias", "normalized_action_text": response["content"],
                         "response_delivered": True})
    (episode / "api_attempts.jsonl").write_text("".join(json.dumps(event) + "\n" for event in attempts))
    metrics.update(provider=stats, token_cap_certified=metrics["total_tokens"] <= metrics["max_tokens"],
                   api_latency_and_retry_wait_exempted=False)
    write_json(episode / "provider_metrics.json", stats)
    write_json(episode / "metrics.json", metrics)

def _completed_documents(tmp_path: Path) -> tuple[Path, Path, Path]:
    report = tmp_path / "report.md"
    disclosure = tmp_path / "ai_disclosure.md"
    reproduction = tmp_path / "REPRODUCE.md"
    report.write_text("# Report\n\nThis is a completed technical report for testing.\n")
    disclosure.write_text(
        "# Disclosure\n\nI used an assistant for tests and verified every change myself.\n"
    )
    reproduction.write_text(
        "# Reproduction\n\nRun python -m eval.run_dev with seed 2026 and six_calls.json.\n"
    )
    return report, disclosure, reproduction


def _test_result(task: Path, selection: str, passed: bool) -> dict:
    metadata = json.loads((task / "task.json").read_text())
    requested = metadata[selection]
    collected = []
    for selector in requested:
        source = task / "grader_tests" / Path(selector).name
        collected.extend(
            f"{selector}::{node.name}" for node in ast.parse(source.read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        )
    code = 0 if passed else 1
    report = {
        "version": 1, "completed": True, "exitstatus": code, "session_exitstatus": code,
        "collected": collected, "collection_issues": [], "deselected": [],
        "tests": [
            {"nodeid": node, "when": phase,
             "outcome": "failed" if not passed and phase == "call" else "passed",
             "wasxfail": False}
            for node in collected for phase in ("setup", "call", "teardown")
        ],
    }
    result = {
        "returncode": code, "process_returncode": code,
        "stdout": f"{len(collected)} {'passed' if passed else 'failed'}\n", "stderr": "",
        "test_report": report,
    }
    if failure := _test_report_error(report, requested, code):
        result["report_error"] = failure
    return result


def _write_episode(task: Path, episode: Path, config_path: Path, index: int, variant: int) -> None:
    """Synthetic offline endpoint fixture in the real runner's artifact format.

    No model is contacted. Outcomes deliberately include visible-only success,
    unresolved nonempty patches, and budget exhaustion with an empty patch.
    These are schema fixtures, not claimed experimental measurements.
    """
    episode.mkdir(parents=True)
    task_data = json.loads((task / "task.json").read_text())
    config = json.loads(config_path.read_text())
    nonempty = (index + variant) % 4 != 1
    visible = nonempty
    resolved = nonempty and (index + variant) % 3 == 2
    termination = "finished" if visible else "budget_exhausted"
    source = next(path for path in sorted((task / "repo").rglob("*.py"))
                  if "tests" not in path.parts and path.name != "__init__.py")
    relative = source.relative_to(task / "repo").as_posix()
    # A parseable source-only patch; acceptance verdicts below are test doubles.
    patch = (
        f"diff --git a/{relative} b/{relative}\n"
        f"--- a/{relative}\n+++ b/{relative}\n@@ -1,1 +1,2 @@\n"
        f"+# Repair candidate inspected by the agent.\n {source.read_text().splitlines()[0]}\n"
    ) if nonempty else ""
    (episode / "patch.diff").write_text(patch)
    budget = {
        "active_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "max_active_seconds": float(config["max_active_seconds"]),
        "max_model_calls": config["max_model_calls"],
        "max_tokens": config["max_total_tokens"],
        "model_calls": 0,
        "test_invocations": 0,
        "test_seconds": 0.0,
        "tool_calls": {},
    }
    events = []

    def event(kind: str, **payload) -> None:
        events.append({"kind": kind, "payload": copy.deepcopy(payload)})

    event("state", status="running", step=0)
    calls = 6 if visible else config["max_model_calls"]
    actions = [
        ("list_files", {"path": "."}),
        ("read_file", {"path": relative}),
        ("search", {"path": ".", "query": "return"}),
        ("write_file" if nonempty else "read_file",
         {"path": relative, **({"content": source.read_text()} if nonempty else {})}),
        ("run_tests", {"command": task_data["visible_command"]}),
    ]
    actions += [("read_file", {"path": relative})] * (calls - len(actions) - 1)
    actions.append(("finish", {"summary": "Inspected the candidate and visible tests."})
                   if visible else ("read_file", {"path": relative}))
    last_observation = ""
    for step, (action, arguments) in enumerate(actions, 1):
        event("model_request", step=step, messages=2 * step)
        budget["model_calls"] += 1
        event("budget", **budget)
        input_tokens, output_tokens = 320 + 40 * step + index, 40 + step + variant
        budget["input_tokens"] += input_tokens
        budget["output_tokens"] += output_tokens
        budget["total_tokens"] = budget["input_tokens"] + budget["output_tokens"]
        budget["active_seconds"] += 0.5 + index / 100
        event("model", step=step, content=json.dumps({
            "action": action, "args": arguments, "thought": "Inspect and verify the repair.",
        }), input_tokens=input_tokens, output_tokens=output_tokens)
        event("budget", **budget)
        if action == "finish":
            event("finish", step=step, summary=arguments["summary"])
            continue
        if action == "run_tests":
            last_observation = "exit_code: 0\n2 passed\n" if visible else "exit_code: 1\n1 failed\n"
            budget["test_invocations"] += 1
            budget["test_seconds"] += 0.2
            budget["active_seconds"] += 0.2
        else:
            last_observation = f"{action} completed for {relative}"
        budget["tool_calls"][action] = budget["tool_calls"].get(action, 0) + 1
        event("tool", step=step, name=action, arguments=arguments, observation=last_observation)
        event("budget", **budget)
    event("state", status=termination, step=calls)
    (episode / "trajectory.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events)
    )
    metrics = {
        "offline_schema_fixture": True,
        **budget,
        "active_seconds": budget["active_seconds"] + 0.01,
        "patch_nonempty": nonempty,
        "resolved": nonempty and visible,
        "visible_tests_passed": visible,
        "termination_reason": termination,
        "run": build_run_identity(task_dir=task, seed=2026, backend="endpoint", config=config_path,
                                  source_root=config_path.parents[2] if config_path.parent.name == "examples"
                                  else config_path.parents[1]),
    }
    write_json(episode / "metrics.json", metrics)
    write_json(episode / "final_state.json", {
        "issue": task_data["issue"],
        "step": calls,
        "status": termination,
        "summary": "Inspected the candidate and visible tests." if visible else "Visible tests failed.",
        "last_observation": last_observation,
        "visible_tests_passed": visible,
    })
    details = {"header_paths": [relative] if nonempty else []}
    if nonempty:
        details.update({
            "changed_paths": [relative], "protected_hits": [], "symlink_hits": [],
            "f2p": _test_result(task, "fail_to_pass", resolved),
            "p2p": _test_result(task, "pass_to_pass", True),
        })
    write_json(episode / "verification.json", {
        "resolved": resolved, "patch_valid": nonempty, "f2p_passed": resolved,
        "p2p_passed": nonempty, "protected_paths_clean": nonempty, "details": details,
    })


def _refresh_batch(directory: Path, split: str, seed: int = 2026) -> dict:
    for episode in directory.iterdir():
        if (episode / "metrics.json").is_file():
            metadata = json.loads((episode / "metrics.json").read_text())
            if metadata.get("offline_schema_fixture") or "provider" not in metadata:
                _provider_fixture(episode)
            _bind_episode(episode)
    records = [
        load_task_record(task, directory / task.name, seed=seed, backend="endpoint",
                         runner_returncode=1 if json.loads((directory / task.name / "metrics.json").read_text())["termination_reason"] == "error" else 0,
                         error="run_agent terminated with error" if json.loads((directory / task.name / "metrics.json").read_text())["termination_reason"] == "error" else "")
        for task in discover_tasks(ROOT / "tasks" / "dev")
    ]
    return write_batch_reports(records, directory, split=split)


def _refresh_paired(paired: Path, seed: int = 2026) -> None:
    write_paired_reports({
        label: _refresh_batch(paired / "conditions" / label, f"public-dev:{label}", seed)["records"]
        for label in ("z_full", "a_greedy", "m_strong")
    }, paired, budget_variable=len({
        tuple(json.loads(path.read_text())[key] for key in
              ("max_model_calls", "max_tokens", "max_active_seconds"))
        for path in paired.rglob("metrics.json")
    }) > 1)


def _write_budget_exhaustion(episode: Path, kind: str, monkeypatch) -> None:
    """Exercise the current response/charge/terminal-log flow with offline IO."""
    original = json.loads((episode / "metrics.json").read_text())
    task = ROOT / "tasks" / "dev" / episode.name
    metadata = json.loads((task / "task.json").read_text())

    class ClockBudget(Budget):
        elapsed = 0.01

        @property
        def active_seconds(self):
            return self.elapsed

    budget = ClockBudget(
        max_model_calls=original["max_model_calls"], max_tokens=original["max_tokens"],
        max_active_seconds=original["max_active_seconds"],
    )

    class OfflineClient:
        def complete(self, messages, seed):
            if kind == "model-time":
                budget.elapsed = budget.max_active_seconds + 1
            if kind == "tokens-after-read" and budget.model_calls == 1:
                return ModelResponse(
                    content='{"action": "list_files", "args": {"path": "."}}',
                    input_tokens=100, output_tokens=20,
                )
            content = json.dumps({
                "action": "run_tests", "args": {"command": metadata["visible_command"]},
            })
            return ModelResponse(
                content=content, input_tokens=budget.max_tokens if kind.startswith("tokens") else 100,
                output_tokens=20,
            )

    def delayed_test(_tools, _command):
        budget.elapsed = budget.max_active_seconds + 1
        return "exit_code: 1\n1 failed\n"

    with monkeypatch.context() as scoped:
        scoped.setattr(ToolRunner, "run_tests", delayed_test)
        state = RepairAgent(OfflineClient(), budget, original["run"]["seed"]).run(
            metadata["issue"], task / "repo", required_test_command=metadata["visible_command"],
        )
    assert state.status == "budget_exhausted"
    events = [item.to_dict() for item in state.events]
    assert sum(item["kind"] == "model" for item in events) == budget.model_calls
    assert [item["kind"] for item in events[-3:]] == ["budget", "error", "state"]
    assert events[-3]["payload"] == budget.metrics()
    if kind == "tool-time":
        assert budget.tool_calls == {"run_tests": 1}
        assert not any(item["kind"] == "tool" for item in events)
    else:
        assert budget.test_invocations == 0  # The over-budget response's action was not executed.
    (episode / "trajectory.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in events)
    )
    (episode / "patch.diff").write_text("")
    write_json(episode / "metrics.json", {
        **budget.metrics(), "run": original["run"], "termination_reason": state.status,
        "visible_tests_passed": state.visible_tests_passed, "patch_nonempty": False,
        "resolved": False,
    })
    write_json(episode / "final_state.json", state.to_dict())
    write_json(episode / "verification.json", validate_patch(task, episode / "patch.diff"))


def _refresh_all(root: Path) -> None:
    _refresh_batch(root / "baseline", "public-dev")
    _refresh_paired(root / "paired")


def _required_results(tmp_path: Path, source_root: Path = ROOT) -> list[tuple[str, Path]]:
    baseline = tmp_path / "evidence" / "baseline"
    paired = tmp_path / "evidence" / "paired"
    # First paired condition intentionally differs from independent Naive, and
    # sorts last in paired_summary.json (the real writer uses sort_keys=True).
    for directory, config_relative, variant in (
        (baseline, "examples/six_calls.json", 0),
        (paired / "conditions" / "z_full", "default.json", 0),
        (paired / "conditions" / "a_greedy", "examples/no_context.json", 1),
        (paired / "conditions" / "m_strong", "examples/no_feedback.json", 2),
    ):
        for index, task in enumerate(discover_tasks(ROOT / "tasks" / "dev")):
            _write_episode(task, directory / task.name,
                           (ROOT if directory == baseline else source_root) / "config" / config_relative, index, variant)
            if source_root != ROOT and directory == baseline:
                identity = json.loads((directory / task.name / "metrics.json").read_text())["run"]
                snapshot_source(directory, identity)
    _refresh_batch(baseline, "public-dev")
    _refresh_paired(paired)
    return [("baseline", baseline), ("paired", paired)]


def _package(tmp_path: Path, results=None, *, source_root: Path = ROOT) -> dict:
    report, disclosure, reproduction = _completed_documents(tmp_path)
    return package_submission(
        source_root=source_root,
        output_path=tmp_path / "submission.zip",
        report_path=report,
        disclosure_path=disclosure,
        reproduction_path=reproduction,
        results=_required_results(tmp_path, source_root) if results is None else results,
        includes=[],
    )


def _rewrite_archive(root: Path, archive: Path) -> None:
    """Rehash mutations so standalone regressions exercise semantic checks too."""
    manifest = json.loads((root / "submission_manifest.json").read_text())
    manifest["files"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file() and path.name != "submission_manifest.json"
    }
    write_json(root / "submission_manifest.json", manifest)
    with zipfile.ZipFile(archive, "w") as handle:
        for path in root.rglob("*"):
            if path.is_file():
                handle.write(path, path.relative_to(root))



def _rehash_envelopes(root):
    # Adversarial semantic checks must get past both hash envelopes. Separate
    # tests exercise unhashed tampering; do not repair any verification claims.
    for path in root.rglob("episode_manifest.json"):
        manifest = json.loads(path.read_text())
        manifest["files"] = episode_hashes(path.parent)
        try:
            metrics = json.loads(path.with_name("metrics.json").read_text())
            if isinstance(metrics, dict) and isinstance(metrics.get("run"), dict):
                manifest["run"] = metrics["run"]
        except (ValueError, OSError):
            pass
        write_json(path, manifest)

def _assert_rejected(tmp_path: Path, mode: str, mutate, match: str) -> None:
    results = _required_results(tmp_path)
    if mode == "package":
        mutate(tmp_path / "evidence")
        _rehash_envelopes(tmp_path / "evidence")
        with pytest.raises(SubmissionError, match=match):
            _package(tmp_path, results)
    else:
        _package(tmp_path, results)
        extracted = tmp_path / "unpacked"
        archive = tmp_path / "submission.zip"
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(extracted)
        mutate(extracted / "evidence")
        _rehash_envelopes(extracted / "evidence")
        _rewrite_archive(extracted, archive)
        with pytest.raises(SubmissionError, match=match):
            check_submission(archive)


def _edit(path: Path, field: str, value) -> None:
    payload = json.loads(path.read_text())
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
    if value is DELETE:
        del target[key]
    else:
        target[key] = value
    # Permit malformed non-finite values in adversarial fixtures.
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")


def test_submission_package_round_trip_excludes_tasks_and_eval(tmp_path: Path) -> None:
    payload = _package(tmp_path)
    archive = tmp_path / "submission.zip"
    assert payload["valid"] is True
    assert payload["metrics_files"] == 48
    assert check_submission(archive)["valid"] is True
    with zipfile.ZipFile(archive) as handle:
        names = handle.namelist()
        manifest = json.loads(handle.read("submission_manifest.json"))
        baseline = json.loads(handle.read(f"evidence/{EPISODE}/metrics.json"))
        verification = json.loads(handle.read(f"evidence/{EPISODE}/verification.json"))
        assert baseline["resolved"] is True and verification["resolved"] is False
        assert handle.read("evidence/baseline/chunk_iterable/patch.diff") == b""
    assert not any(name.startswith(("tasks/", "tests/")) for name in names)
    assert "eval/run_identity.py" in names
    for filename in EPISODE_FILES:
        assert f"evidence/{EPISODE}/{filename}" in names
    assert "submission_manifest.json" not in manifest["files"]
    before = archive.read_bytes()
    _package(tmp_path, [("baseline", tmp_path / "evidence" / "baseline"),
                        ("paired", tmp_path / "evidence" / "paired")])
    assert archive.read_bytes() == before
    # Exercise the CLI with this test's trusted release manifest, offline and
    # outside the release cwd. This is a test-only monkeypatch, not a CLI bypass.
    command = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "from tools import submission; "
        f"submission.INTEGRITY_MANIFEST = Path({str(submission.INTEGRITY_MANIFEST)!r}); "
        "submission.main()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command, "check", str(archive)],
        cwd=tmp_path, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["valid"] is True


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize("episode", [EPISODE, PAIRED_EPISODE])
@pytest.mark.parametrize("filename", EPISODE_FILES)
def test_rejects_missing_episode_artifact(tmp_path, mode, episode, filename) -> None:
    _assert_rejected(tmp_path, mode, lambda root: (root / episode / filename).unlink(), filename)


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize("relative", [
    "baseline/summary.json", "baseline/tasks.csv",
    "paired/conditions/a_greedy/summary.json", "paired/conditions/z_full/tasks.csv",
    "paired/paired_summary.json", "paired/paired_tasks.csv",
])
def test_rejects_missing_experiment_artifact(tmp_path, mode, relative) -> None:
    _assert_rejected(tmp_path, mode, lambda root: (root / relative).unlink(),
                     "baseline|paired ablations|condition summary|CSV")


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize(("relative", "field", "value", "match"), [
    ("baseline/summary.json", "records.0", DELETE, "records"),
    ("paired/conditions/a_greedy/summary.json", "records.0", DELETE, "records"),
    ("baseline/summary.json", "records.0.task_id", "task-00", "actual 12"),
    ("baseline/summary.json", "records.0.task_id", "dev_chunk_iterable", "actual 12"),
    ("baseline/summary.json", "records.0.task_name", "../casefold_headers", "task_name"),
    ("baseline/summary.json", "records.0.seed", 2027, "seed"),
    ("baseline/summary.json", "records.0.backend", "replay", "backend"),
    ("baseline/summary.json", "records.0.runner_returncode", 1, "runner_returncode"),
    ("baseline/summary.json", "records.0.error", "runner failed", "error"),
    ("baseline/summary.json", "records.0.resolved", True, "resolved"),
    ("baseline/summary.json", "records.0.output_dir", "/tmp/other-task", "output_dir"),
    ("baseline/summary.json", "tasks", 11, "tasks"),
    ("baseline/summary.json", "resolved", 12, "resolved"),
    ("baseline/summary.json", "runner_failures", 1, "runner_failures"),
    ("baseline/summary.json", "resolve_rate_wilson_95.low", 0.999, "wilson"),
    ("baseline/summary.json", "cost.total_tokens.mean", 1.0, "cost.total_tokens.mean"),
    ("baseline/summary.json", "cost.test_seconds", DELETE, "test_seconds"),
    ("baseline/summary.json", "task_order.0", "task-00", "task_order"),
    (f"{EPISODE}/metrics.json", "run.seed", 2027, "seed"),
    (f"{EPISODE}/metrics.json", "run.backend", "replay", "run identity"),
    (f"{EPISODE}/metrics.json", "run.config_sha256", "0" * 64, "config_sha256"),
    (f"{EPISODE}/metrics.json", "run.task_input_sha256", "0" * 64, "run identity"),
    (f"{EPISODE}/metrics.json", "run.model_id", "other-model", "requested_model"),
    (f"{EPISODE}/metrics.json", "run.source_sha256", "0" * 64, "source_sha256"),
    (f"{EPISODE}/metrics.json", "total_tokens", 1, "total_tokens"),
    (f"{EPISODE}/metrics.json", "model_calls", 5, "budget|count"),
    (f"{EPISODE}/metrics.json", "model_calls", True, "model_calls"),
    (f"{EPISODE}/metrics.json", "input_tokens", DELETE, "input_tokens"),
    (f"{EPISODE}/metrics.json", "visible_tests_passed", DELETE, "visible_tests_passed"),
    (f"{EPISODE}/metrics.json", "active_seconds", float("nan"), "JSON"),
    (f"{EPISODE}/metrics.json", "active_seconds", float("inf"), "JSON"),
    (f"{EPISODE}/metrics.json", "max_model_calls", 12, "max_model_calls"),
    (f"{EPISODE}/metrics.json", "max_tokens", 999999, "max_tokens"),
    (f"{EPISODE}/metrics.json", "tool_calls.read_file", 999, "tool counts"),
    (f"{EPISODE}/metrics.json", "test_invocations", 0, "budget|test_invocations"),
    (f"{EPISODE}/metrics.json", "termination_reason", "error", "status"),
    (f"{EPISODE}/final_state.json", "issue", "a different task", "issue"),
    (f"{EPISODE}/final_state.json", "status", "running", "status"),
    (f"{EPISODE}/final_state.json", "step", 0, "step"),
    (f"{EPISODE}/final_state.json", "visible_tests_passed", False, "visible_tests_passed"),
    (f"{EPISODE}/verification.json", "f2p_passed", "false", "f2p_passed"),
    (f"{EPISODE}/verification.json", "details", DELETE, "details"),
    (f"{EPISODE}/verification.json", "details.p2p", DELETE, "p2p"),
    (f"{EPISODE}/verification.json", "details.p2p.returncode", 1, "returncode"),
    (f"{EPISODE}/verification.json", "details.p2p.test_report", DELETE, "test_report"),
    (f"{EPISODE}/verification.json", "details.p2p.test_report.tests", [], "returncode"),
    (f"{EPISODE}/verification.json", "details.p2p.test_report.completed", False, "returncode"),
    (f"{EPISODE}/verification.json", "details.p2p.test_report.tests.1.outcome",
     "skipped", "returncode"),
    (f"{EPISODE}/verification.json", "details.p2p.test_report.tests.1.wasxfail",
     True, "returncode"),
    (f"{EPISODE}/verification.json", "resolved", True, "resolved"),
    (f"{EPISODE}/verification.json", "protected_paths_clean", False, "protected paths"),
    ("paired/paired_summary.json", "seed", 2027, "seed"),
    ("paired/paired_summary.json", "task_order.0", "task-00", "task_order"),
    ("paired/paired_summary.json", "conditions.a_greedy.tasks", 11, "tasks"),
    ("paired/paired_summary.json", "conditions.a_greedy.mean_total_tokens", 1, "mean_total_tokens"),
    ("paired/paired_summary.json", "comparisons_to_first_condition.a_greedy.wins", 99, "wins"),
    ("paired/paired_summary.json", "comparisons_to_first_condition.a_greedy.losses", 99, "losses"),
    ("paired/paired_summary.json", "comparisons_to_first_condition.a_greedy.ties", 99, "ties"),
    ("paired/paired_summary.json", "comparisons_to_first_condition.a_greedy.baseline",
     "a_greedy", "baseline"),
    ("paired/paired_summary.json", "comparisons_to_first_condition.a_greedy.resolve_rate_delta",
     1.0, "resolve_rate_delta"),
    ("paired/paired_summary.json", "conditions.a_greedy", DELETE, "paired ablations"),
])
def test_rejects_inconsistent_evidence(tmp_path, mode, relative, field, value, match) -> None:
    _assert_rejected(tmp_path, mode, lambda root: _edit(root / relative, field, value), match)


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize(("filename", "content"), [
    ("metrics.json", b"[]\n"),
    ("metrics.json", b'{"run": {}, "run": {}}\n'),
    ("final_state.json", b"\xff"),
    ("verification.json", b"{not json}\n"),
    ("trajectory.jsonl", b""),
    ("trajectory.jsonl", b"{}\n"),
    ("trajectory.jsonl", b'{"kind": "model", "payload": []}\n'),
    ("trajectory.jsonl", b'{"kind": "model", "payload": {"step": 1}}\n'),
    ("patch.diff", b"\xff"),
    ("patch.diff", b"a claimed valid patch with no diff headers\n"),
])
def test_rejects_unparseable_or_skeletal_episode(tmp_path, mode, filename, content) -> None:
    episode = EPISODE if content.startswith(b"a claimed valid patch") else PAIRED_EPISODE
    _assert_rejected(tmp_path, mode,
                     lambda root: (root / episode / filename).write_bytes(content),
                     "JSON|UTF-8|trajectory|kind and payload|model content|header_paths|patch_sha256")


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize("kind", ["model_request", "model", "tool", "budget", "state"])
def test_rejects_missing_trajectory_records(tmp_path, mode, kind) -> None:
    def mutate(root):
        path = root / EPISODE / "trajectory.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["kind"] != kind))
    _assert_rejected(tmp_path, mode, mutate, "request|count|missing")


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize(("relative", "field", "value"), [
    ("baseline/tasks.csv", "task_id", "task-00"),
    ("baseline/tasks.csv", "resolved", "True"),
    ("baseline/tasks.csv", "total_tokens", "0"),
    ("paired/paired_tasks.csv", "task_id", "task-00"),
    ("paired/paired_tasks.csv", "a_greedy.resolved", "True"),
    ("paired/paired_tasks.csv", "a_greedy.model_calls", "99"),
    ("paired/paired_tasks.csv", "a_greedy.total_tokens", "99"),
    ("paired/paired_tasks.csv", "a_greedy.active_seconds", "NaN"),
])
def test_rejects_tampered_csv_cells(tmp_path, mode, relative, field, value) -> None:
    def mutate(root):
        path = root / relative
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            fields, rows = reader.fieldnames, list(reader)
        rows[0][field] = value
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    _assert_rejected(tmp_path, mode, mutate, field)


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize("change", ["missing-column", "duplicate-column", "missing-row", "extra-row"])
def test_rejects_malformed_paired_matrix(tmp_path, mode, change) -> None:
    def mutate(root):
        path = root / "paired" / "paired_tasks.csv"
        with path.open(newline="") as handle:
            rows = list(csv.reader(handle))
        if change == "missing-column":
            rows = [row[:-1] for row in rows]
        elif change == "duplicate-column":
            rows[0][-1] = rows[0][-2]
        elif change == "missing-row":
            rows.pop()
        else:
            rows.append(rows[-1])
        with path.open("w", newline="") as handle:
            csv.writer(handle).writerows(rows)
    _assert_rejected(tmp_path, mode, mutate, "CSV")


@pytest.mark.parametrize("mode", ["package", "check"])
def test_rejects_unreported_episode(tmp_path, mode) -> None:
    def mutate(root):
        shutil.copytree(root / EPISODE, root / "baseline" / "task-00")
    _assert_rejected(tmp_path, mode, mutate, "orphan")


def test_rejects_changed_packaged_config(tmp_path) -> None:
    _package(tmp_path)
    extracted = tmp_path / "unpacked"
    archive = tmp_path / "submission.zip"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extracted)
    _edit(extracted / "config" / "examples" / "no_context.json", "temperature", 0.1)
    _rewrite_archive(extracted, archive)
    with pytest.raises(SubmissionError, match="config_sha256"):
        check_submission(archive)


def test_requires_naive_baseline_even_with_complete_other_config_evidence(tmp_path) -> None:
    results = _required_results(tmp_path)
    for index, task in enumerate(discover_tasks(ROOT / "tasks" / "dev")):
        _write_episode(task, tmp_path / "replacement" / task.name,
                       ROOT / "config" / "default.json", index, 0)
    _refresh_batch(tmp_path / "replacement", "public-dev")
    results[0] = ("baseline", tmp_path / "replacement")
    with pytest.raises(SubmissionError, match="Naive baseline"):
        _package(tmp_path, results)


def test_paired_family_must_use_independent_naive_declared_seed(tmp_path) -> None:
    results = _required_results(tmp_path)
    paired = results[1][1]
    for path in paired.rglob("metrics.json"):
        _edit(path, "run.seed", 2027)
    _refresh_paired(paired, seed=2027)
    with pytest.raises(SubmissionError, match="paired seed/task_order/.*independent Naive baseline"):
        _package(tmp_path, results)


def test_paired_family_can_differ_from_independent_naive_config_and_outcomes(tmp_path) -> None:
    results = _required_results(tmp_path)
    paired = results[1][1]
    # z_full already uses default.json instead of the independent six_calls config.
    episode = paired / "conditions" / "z_full" / "csv_records"
    verification = json.loads((episode / "verification.json").read_text())
    assert verification["resolved"] is True
    verification["f2p_passed"] = verification["resolved"] = False
    verification["details"]["f2p"] = _test_result(
        ROOT / "tasks" / "dev" / "csv_records", "fail_to_pass", False,
    )
    write_json(episode / "verification.json", verification)
    _refresh_paired(paired)
    assert _package(tmp_path, results)["valid"] is True


def test_preserves_custom_harness_modules_config_fields_and_event_kinds(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for relative in ("run_agent.py", "harness", "eval", "config", "requirements.lock", "pyproject.toml"):
        item = ROOT / relative
        if item.is_dir():
            shutil.copytree(item, source / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, source / relative)
    custom = source / "harness" / "custom_strategy.py"
    custom.write_text('raise RuntimeError("submission checking must not import student code")\n')
    for relative in ("run_agent.py", "harness/agent.py", "harness/tools.py", "harness/patch.py",
                     "harness/localize.py", "harness/state.py"):
        with (source / relative).open("a") as handle:
            handle.write("\n# Student-owned implementation change.\n")
    _edit(source / "config" / "examples" / "no_feedback.json", "custom_selector", "search_then_verify")
    results = _required_results(tmp_path, source)
    trajectory = results[1][1] / "conditions" / "m_strong" / TASK_NAME / "trajectory.jsonl"
    rows = [json.loads(line) for line in trajectory.read_text().splitlines()]
    for row in rows:
        payload = row["payload"]
        if row["kind"] == "tool" and payload["name"] == "search":
            payload["name"] = "custom_search"
        if row["kind"] == "budget" and "search" in payload["tool_calls"]:
            payload["tool_calls"]["custom_search"] = payload["tool_calls"].pop("search")
    trajectory.write_text("".join(json.dumps(row) + "\n" for row in rows))
    metrics_path = trajectory.with_name("metrics.json")
    metrics = json.loads(metrics_path.read_text())
    metrics["tool_calls"]["custom_search"] = metrics["tool_calls"].pop("search")
    write_json(metrics_path, metrics)
    with trajectory.open("a") as handle:
        handle.write('{"kind": "custom_analysis", "payload": {"selected": "candidate_2"}}\n')
    _refresh_all(tmp_path / "evidence")
    assert _package(tmp_path, results, source_root=source)["valid"] is True
    with zipfile.ZipFile(tmp_path / "submission.zip") as handle:
        assert handle.read("harness/custom_strategy.py") == custom.read_bytes()


def test_rejects_credentials_but_accepts_public_localhost_placeholder(tmp_path) -> None:
    # The positive round trip already packages the client's public placeholder.
    results = _required_results(tmp_path)
    disclosure = _completed_documents(tmp_path)[1]
    disclosure.write_text('# Disclosure\n\napi_key = "personal-token-do-not-submit"\n')
    with pytest.raises(SubmissionError, match="credential"):
        package_submission(
            source_root=ROOT, output_path=tmp_path / "submission.zip",
            report_path=tmp_path / "report.md", disclosure_path=disclosure,
            reproduction_path=tmp_path / "REPRODUCE.md", results=results, includes=[],
        )


def test_manifest_still_rejects_unhashed_tampering(tmp_path) -> None:
    _package(tmp_path)
    archive = tmp_path / "submission.zip"
    with zipfile.ZipFile(archive) as handle:
        members = {name: handle.read(name) for name in handle.namelist()}
    members[f"evidence/{EPISODE}/patch.diff"] += b"# changed after packaging\n"
    with zipfile.ZipFile(archive, "w") as handle:
        for name, data in members.items():
            handle.writestr(name, data)
    with pytest.raises(SubmissionError, match="hashes"):
        check_submission(archive)


def test_accepts_zero_resolved_runs_and_empty_failed_patches(tmp_path) -> None:
    results = _required_results(tmp_path)
    for path in (tmp_path / "evidence").rglob("verification.json"):
        verification = json.loads(path.read_text())
        verification["resolved"] = False
        verification["f2p_passed"] = False
        if verification["patch_valid"]:
            verification["details"]["f2p"] = _test_result(
                ROOT / "tasks" / "dev" / path.parent.name, "fail_to_pass", False,
            )
        write_json(path, verification)
    _refresh_batch(results[0][1], "public-dev")
    _refresh_paired(results[1][1])
    assert _package(tmp_path, results)["valid"] is True


def _passing_patch_with_overshoot(episode: Path, counter: str) -> None:
    """Keep a passing patch and expose a budget failure in coherent run evidence."""
    metrics = json.loads((episode / "metrics.json").read_text())
    state = json.loads((episode / "final_state.json").read_text())
    assert json.loads((episode / "verification.json").read_text())["resolved"] is True
    assert metrics["resolved"] is True
    rows = [
        json.loads(line) for line in (episode / "trajectory.jsonl").read_text().splitlines()
    ]
    rows = [
        row for row in rows if row["kind"] != "finish"
        and not (row["kind"] == "state" and row["payload"]["status"] == "finished")
    ]
    budget_keys = next(row["payload"].keys() for row in rows if row["kind"] == "budget")
    limit = {
        "model_calls": "max_model_calls", "total_tokens": "max_tokens",
        "active_seconds": "max_active_seconds",
    }[counter]
    previous_calls = metrics["model_calls"]
    excess = metrics[limit] + 1 - metrics[counter]
    if counter == "total_tokens":
        metrics["input_tokens"] += excess
        last_model = max(index for index, row in enumerate(rows) if row["kind"] == "model")
        rows[last_model]["payload"]["input_tokens"] += excess
        # The charged terminal snapshot replaces the post-response snapshot.
        rows = rows[:last_model + 1]
    metrics[counter] += excess
    metrics["termination_reason"] = "budget_exhausted"
    if counter == "model_calls":
        for call in range(previous_calls + 1, metrics["model_calls"] + 1):
            rows.append({"kind": "model_request", "payload": {"step": call, "messages": 2 * call}})
            if call < metrics["model_calls"]:
                snapshot = {field: metrics[field] for field in budget_keys}
                snapshot["model_calls"] = call
                rows.extend([
                    {"kind": "budget", "payload": snapshot},
                    {"kind": "error", "payload": {
                        "step": call, "message": "Request failed before returning token usage",
                    }},
                ])
        state["step"] = metrics["model_calls"]
    state["status"] = "budget_exhausted"
    state["last_observation"] = f"{counter} budget exceeded"
    rows.extend([
        {"kind": "budget", "payload": {field: metrics[field] for field in budget_keys}},
        {"kind": "error", "payload": {
            "step": state["step"], "message": state["last_observation"],
        }},
        {"kind": "state", "payload": {"step": state["step"], "status": state["status"]}},
    ])
    write_json(episode / "metrics.json", metrics)
    write_json(episode / "final_state.json", state)
    (episode / "trajectory.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize("batch", ["baseline", "paired/conditions/z_full"])
@pytest.mark.parametrize("counter", ["model_calls", "total_tokens", "active_seconds"])
def test_passing_patch_overshoot_is_accepted_as_unresolved_evidence(tmp_path, batch, counter) -> None:
    results = _required_results(tmp_path)
    root = tmp_path / "evidence"
    episode = root / batch / "csv_records"
    raw_verification = (episode / "verification.json").read_bytes()
    _passing_patch_with_overshoot(episode, counter)
    _refresh_all(root)
    summary = json.loads((root / batch / "summary.json").read_text())
    record = next(item for item in summary["records"] if item["task_id"] == "dev_csv_records")
    assert record["resolved"] is False
    assert record["patch_valid"] is record["f2p_passed"] is record["p2p_passed"] is True
    assert _package(tmp_path, results)["valid"] is True
    assert check_submission(tmp_path / "submission.zip")["valid"] is True
    with zipfile.ZipFile(tmp_path / "submission.zip") as archive:
        assert archive.read(f"evidence/{batch}/csv_records/verification.json") == raw_verification
        archived_summary = json.loads(archive.read(f"evidence/{batch}/summary.json"))
        archived_record = next(
            item for item in archived_summary["records"] if item["task_id"] == "dev_csv_records"
        )
        assert archived_record["resolved"] is False
    with (root / batch / "tasks.csv").open(newline="") as handle:
        row = next(row for row in csv.DictReader(handle) if row["task_id"] == "dev_csv_records")
    assert row["resolved"] == "False"
    if batch != "baseline":
        with (root / "paired" / "paired_tasks.csv").open(newline="") as handle:
            row = next(row for row in csv.DictReader(handle) if row["task_id"] == "dev_csv_records")
        assert row["z_full.resolved"] == "False"


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize("batch", ["baseline", "paired/conditions/z_full"])
@pytest.mark.parametrize("counter", ["model_calls", "total_tokens", "active_seconds"])
def test_rejects_credit_for_passing_patch_overshoot(tmp_path, mode, batch, counter) -> None:
    def mutate(root):
        _passing_patch_with_overshoot(root / batch / "csv_records", counter)
        _refresh_all(root)
        summary = json.loads((root / batch / "summary.json").read_text())
        for record in summary["records"]:
            if record["task_id"] == "dev_csv_records":
                record["resolved"] = True
        # Regenerate every aggregate and CSV so rejection cannot rely on a stale
        # count or matrix: the record itself must obey the shared scoring predicate.
        write_batch_reports(summary["records"], root / batch, split=summary["split"])
        paired = root / "paired"
        write_paired_reports({
            label: json.loads((paired / "conditions" / label / "summary.json").read_text())["records"]
            for label in ("z_full", "a_greedy", "m_strong")
        }, paired, budget_variable=True)
    _assert_rejected(tmp_path, mode, mutate, r"record dev_csv_records\.resolved")


@pytest.mark.parametrize("kind", ["tokens", "tokens-after-read", "model-time", "tool-time"])
@pytest.mark.parametrize("legacy_diagnostic", [False, True])
def test_accepts_budget_exhaustion_after_an_inflight_operation(
    tmp_path, monkeypatch, kind, legacy_diagnostic,
) -> None:
    results = _required_results(tmp_path)
    root = tmp_path / "evidence"
    for relative in (EPISODE, PAIRED_EPISODE):
        _write_budget_exhaustion(root / relative, kind, monkeypatch)
    _refresh_all(root)
    if legacy_diagnostic:
        for relative in (EPISODE, PAIRED_EPISODE):
            directory = (root / relative).parent
            summary = json.loads((directory / "summary.json").read_text())
            for record in summary["records"]:
                if record["task_name"] == TASK_NAME:
                    record["outcome"] = "error"
                    record["error"] = (
                        "metrics.json total_tokens exceeds max_tokens" if kind.startswith("tokens")
                        else "metrics.json active_seconds exceeds max_active_seconds"
                    )
            write_batch_reports(summary["records"], directory, split=summary["split"])
            episode = root / relative
            metadata = json.loads((episode / "metrics.json").read_text())
            error = next(record["error"] for record in summary["records"] if record["task_name"] == TASK_NAME)
            write_episode_manifest(episode, metadata["run"], 0, error)
    assert _package(tmp_path, results)["valid"] is True
    assert check_submission(tmp_path / "submission.zip")["valid"] is True


@pytest.mark.parametrize("limit", ["max_total_tokens", "max_active_seconds"])
@pytest.mark.parametrize("infrastructure_error", [False, True])
def test_actual_runner_first_request_with_tiny_positive_budget(
    tmp_path, monkeypatch, limit, infrastructure_error,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for relative in ("run_agent.py", "harness", "eval", "config", "requirements.lock",
                     "pyproject.toml"):
        item = ROOT / relative
        if item.is_dir():
            shutil.copytree(item, source / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, source / relative)
    config = source / "config" / "examples" / "no_context.json"
    if not infrastructure_error:
        _edit(config, limit, 1 if limit == "max_total_tokens" else 0.001)
    results = _required_results(tmp_path, source_root=source)
    condition = results[1][1] / "conditions" / "a_greedy"
    shutil.rmtree(condition)  # Replace this test's synthetic condition with actual runner outputs.
    budgets = []

    class ClockBudget(Budget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.elapsed = 0.0
            budgets.append(self)

        @property
        def active_seconds(self):
            return self.elapsed

    from harness.model_client import OpenAICompatibleModelClient

    class OfflineTransport:
        def post(self, body, key, timeout):
            if infrastructure_error:
                raise ConnectionError("offline fixture: endpoint unavailable")
            raise AssertionError("tiny budget must stop before HTTP")

    def OfflineEndpoint(**kwargs):
        return OpenAICompatibleModelClient(**kwargs, transport=OfflineTransport(),
                                           sleep=lambda seconds: None, clock=lambda: budgets[-1].elapsed)

    monkeypatch.setenv("HW2_MODEL_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("HW2_MODEL_ID", "optional-model")
    monkeypatch.setenv("HW2_MODEL_API_KEY", "local-development-only")
    monkeypatch.setattr(run_agent, "Budget", ClockBudget)
    monkeypatch.setattr(run_agent, "OpenAICompatibleModelClient", OfflineEndpoint)
    expected_status = "error" if infrastructure_error else "budget_exhausted"
    for task in discover_tasks(ROOT / "tasks" / "dev"):
        episode = condition / task.name
        monkeypatch.setattr(sys, "argv", [
            "run_agent.py", "--task_dir", str(task), "--output_dir", str(episode),
            "--seed", "2026", "--backend", "endpoint", "--config", str(config),
        ])
        # Real worktree initialization, RepairAgent loop, charging, serialization,
        # and run identity; only endpoint IO and elapsed time are offline doubles.
        if infrastructure_error:
            with pytest.raises(SystemExit) as failure:
                run_agent.main()
            assert failure.value.code == 1
        else:
            run_agent.main()
        write_json(episode / "verification.json", validate_patch(task, episode / "patch.diff"))
        metrics = json.loads((episode / "metrics.json").read_text())
        rows = [json.loads(line) for line in (episode / "trajectory.jsonl").read_text().splitlines()]
        assert rows[0]["kind"] == "state" and rows[-1]["kind"] == "state"
        assert metrics["model_calls"] == 1
        assert metrics["total_tokens"] == (metrics["max_tokens"] if infrastructure_error else 0)
        assert metrics["provider"]["http_attempts"] == int(infrastructure_error)
        assert metrics["provider"]["usage_accounting_complete"] is not infrastructure_error
        assert metrics["token_cap_certified"] is not infrastructure_error
        assert metrics["termination_reason"] == expected_status
        assert json.loads((episode / "final_state.json").read_text())["status"] == expected_status
        assert (episode / "patch.diff").read_text() == ""
    _refresh_all(tmp_path / "evidence")
    assert _package(tmp_path, results, source_root=source)["valid"] is True
    assert check_submission(tmp_path / "submission.zip")["valid"] is True


@pytest.mark.parametrize("kind", ["tokens", "model-time"])
def test_accepts_fully_logged_budget_exhaustion(tmp_path, monkeypatch, kind) -> None:
    results = _required_results(tmp_path)
    root = tmp_path / "evidence"
    episode = root / EPISODE
    _write_budget_exhaustion(episode, kind, monkeypatch)
    metrics = json.loads((episode / "metrics.json").read_text())
    trajectory = episode / "trajectory.jsonl"
    rows = [json.loads(line) for line in trajectory.read_text().splitlines()]
    # Use the emitted current-runtime evidence without synthesizing missing logs.
    response = next(row["payload"] for row in rows if row["kind"] == "model")
    assert response["input_tokens"] + response["output_tokens"] == metrics["total_tokens"]
    assert rows[-3]["kind"] == "budget"
    assert rows[-3]["payload"]["total_tokens"] == metrics["total_tokens"]
    _refresh_all(root)
    assert _package(tmp_path, results)["valid"] is True


@pytest.mark.parametrize("mode", ["package", "check"])
@pytest.mark.parametrize("tamper", [
    "missing-response", "missing-prior-model", "missing-final-budget",
    "missing-verification", "finished", "extra-tool",
])
def test_budget_exhaustion_does_not_excuse_missing_or_inconsistent_records(
    tmp_path, monkeypatch, mode, tamper,
) -> None:
    def mutate(root):
        episode = root / EPISODE
        kind = (
            "tool-time" if tamper == "extra-tool"
            else "tokens-after-read" if tamper == "missing-prior-model" else "tokens"
        )
        _write_budget_exhaustion(episode, kind, monkeypatch)
        if tamper in {"missing-response", "missing-prior-model", "missing-final-budget"}:
            trajectory = episode / "trajectory.jsonl"
            rows = [json.loads(line) for line in trajectory.read_text().splitlines()]
            if tamper == "missing-final-budget":
                del rows[max(index for index, row in enumerate(rows) if row["kind"] == "budget")]
            else:
                del rows[next(index for index, row in enumerate(rows) if row["kind"] == "model")]
            trajectory.write_text("".join(json.dumps(row) + "\n" for row in rows))
        elif tamper == "missing-verification":
            (episode / "verification.json").unlink()
        elif tamper == "finished":
            _edit(episode / "metrics.json", "termination_reason", "finished")
            _edit(episode / "final_state.json", "status", "finished")
        else:
            _edit(episode / "metrics.json", "tool_calls.run_tests", 2)
        _refresh_all(root)
    _assert_rejected(tmp_path, mode, mutate,
                     "missing model|verification.json|without valid termination|tool counts|"
                     "input_tokens|output_tokens|final budget")


@pytest.mark.parametrize("mode", ["package", "check"])
def test_finished_episode_cannot_claim_soft_budget_overshoot(tmp_path, mode) -> None:
    _assert_rejected(
        tmp_path, mode,
        lambda root: _edit(root / EPISODE / "metrics.json", "active_seconds", 1801.0),
        "without valid termination",
    )


def test_submission_rejects_incomplete_disclosure(tmp_path: Path) -> None:
    disclosure = tmp_path / "ai_disclosure.md"
    disclosure.write_text("# Disclosure\n\nTODO\n")
    with pytest.raises(SubmissionError, match="incomplete|placeholder"):
        package_submission(
            source_root=ROOT, output_path=tmp_path / "submission.zip",
            report_path=disclosure, disclosure_path=disclosure,
            reproduction_path=disclosure, results=[], includes=[],
        )


def test_submission_rejects_self_reported_hidden_score(tmp_path: Path) -> None:
    results = _required_results(tmp_path)
    _edit(results[0][1] / "summary.json", "hidden_score", 50)
    with pytest.raises(SubmissionError, match="hidden_score"):
        _package(tmp_path, results)


def _retry_episode(tmp_path):
    episode = tmp_path / TASK_NAME
    _write_episode(ROOT / "tasks" / "dev" / TASK_NAME, episode,
                   ROOT / "config" / "examples" / "six_calls.json", 0, 0)
    _provider_fixture(episode)
    metrics = json.loads((episode / "metrics.json").read_text())
    attempts = [json.loads(line) for line in (episode / "api_attempts.jsonl").read_text().splitlines()]
    retry_start = {**attempts[0], "attempt": 1}
    retry_end = {**retry_start, "phase": "response_received", "http_status": 429,
                 "request_seconds": 0.0, "usage_status": "unknown_reserved",
                 "usage_raw": None, "charged_input_tokens": 0, "charged_output_tokens": 100,
                 "unknown_usage_reservation_tokens": 100}
    for event in attempts:
        event["attempt"] += 1
    attempts = [retry_start, retry_end, *attempts]
    (episode / "api_attempts.jsonl").write_text("".join(json.dumps(event) + "\n" for event in attempts))
    provider = metrics["provider"]
    provider.update(retries=1, http_attempts=provider["http_attempts"] + 1,
                    http_429=1, unknown_usage_attempts=1, reserved_unknown_tokens=100,
                    usage_accounting_complete=False)
    metrics.update(model_calls=metrics["model_calls"] + 1, output_tokens=metrics["output_tokens"] + 100,
                   total_tokens=metrics["total_tokens"] + 100, token_cap_certified=False)
    write_json(episode / "provider_metrics.json", provider)
    return episode, metrics


def test_retry_usage_is_retained_but_never_certified(tmp_path):
    episode, metrics = _retry_episode(tmp_path)
    submission._check_provider(episode, metrics)
    assert metrics["model_calls"] == 7 and metrics["provider"]["http_attempts"] == 7


@pytest.mark.parametrize("nested", ["unavailable", None, -2, True, [], 9])
def test_unknown_usage_reasoning_normalizes_malformed_nested_counts(tmp_path, nested):
    episode, metrics = _retry_episode(tmp_path)
    path = episode / "api_attempts.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[1]["usage_raw"] = {
        "prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": nested},
    }
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    expected = max(5, nested if type(nested) is int and nested >= 0 else 0)
    metrics["provider"].update(reported_reasoning_tokens=expected, reasoning_usage_reported_attempts=1)
    write_json(episode / "provider_metrics.json", metrics["provider"])
    submission._check_provider(episode, metrics)
    assert metrics["token_cap_certified"] is False
    # The same normalized count must still participate in the lower bound.
    events[1]["usage_raw"]["prompt_tokens"] = 80
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    with pytest.raises(SubmissionError, match="reported lower bound"):
        submission._check_provider(episode, metrics)


@pytest.mark.parametrize("tamper", ["certified", "uncounted-retry", "usage-complete", "usage-count"])
def test_retry_accounting_rejects_false_certification(tmp_path, tamper):
    episode, metrics = _retry_episode(tmp_path)
    if tamper == "certified":
        metrics["token_cap_certified"] = True
    elif tamper == "uncounted-retry":
        metrics["model_calls"] -= 1
    elif tamper == "usage-complete":
        metrics["provider"]["usage_accounting_complete"] = True
    else:
        metrics["provider"]["unknown_usage_attempts"] = 0
    write_json(episode / "provider_metrics.json", metrics["provider"])
    with pytest.raises(SubmissionError, match="token_cap_certified|retries count|usage"):
        submission._check_provider(episode, metrics)


@pytest.mark.parametrize("text", [
    '{"api_key": "nvapi-abcdefghijklmnopqrstuv"}',
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    'api_key = "personal-secret-value"',
])
def test_secret_boundary_rejects_actual_tokens(tmp_path, text):
    path = tmp_path / "logs"
    path.mkdir()
    (path / "api_attempts.jsonl").write_text(text)
    with pytest.raises(SubmissionError, match="credential"):
        submission._scan_for_secrets(path)
    (path / "api_attempts.jsonl").write_text('{"api_key": "[REDACTED]", "error": "HTTP 401"}')
    submission._scan_for_secrets(path)


def test_staff_reverification_uses_patches_without_student_keys(tmp_path, monkeypatch):
    results = _required_results(tmp_path)
    for metrics_path in (tmp_path / "evidence").rglob("metrics.json"):
        episode = metrics_path.parent
        (episode / "patch.diff").write_text("")
        metrics = json.loads(metrics_path.read_text())
        metrics.update(patch_nonempty=False, resolved=False)
        write_json(metrics_path, metrics)
        write_json(episode / "verification.json", validate_patch(
            ROOT / "tasks" / "dev" / episode.name, episode / "patch.diff"))
    _refresh_all(tmp_path / "evidence")
    _package(tmp_path, results)
    from harness import model_client
    monkeypatch.setattr(model_client, "resolve_model_config",
                        lambda: pytest.fail("offline reverify must not resolve credentials"))
    monkeypatch.delenv("HW2_MODEL_API_KEY", raising=False)
    result = submission.reverify_submission(tmp_path / "submission.zip", tmp_path / "fresh")
    assert result["episodes"] == 48 and result["model_calls"] == 0
    assert result["verdict_mismatches"] == 0
    assert len(list((tmp_path / "fresh").rglob("verification.json"))) == 48


def test_protected_contract_cannot_be_changed(tmp_path):
    for relative in [*source_files(ROOT), "config/examples/six_calls.json"]:
        target = tmp_path / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    root = tmp_path / "source"
    submission._check_integrity(root)
    with (root / "harness" / "budget.py").open("a") as handle:
        handle.write("\n# Mutated protected budget.\n")
    with pytest.raises(SubmissionError, match="staff-owned file was modified"):
        submission._check_integrity(root)


def test_source_snapshot_excludes_configs_but_still_requires_protected_code(tmp_path):
    for relative in source_files(ROOT):
        target = tmp_path / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    root = tmp_path / "source"
    submission._check_integrity(root, source_snapshot=True)
    with pytest.raises(SubmissionError, match="config/examples/six_calls.json"):
        submission._check_integrity(root)
    (root / "harness/model_client.py").unlink()
    with pytest.raises(SubmissionError, match="harness/model_client.py"):
        submission._check_integrity(root, source_snapshot=True)


@pytest.mark.parametrize("tamper", [None, "missing-prior-response", "false-budget-error"])
def test_undelivered_over_budget_api_response_has_explicit_evidence(tmp_path, tamper):
    from harness.model_client import account_usage
    episode = tmp_path / TASK_NAME
    _write_episode(ROOT / "tasks" / "dev" / TASK_NAME, episode,
                   ROOT / "config" / "examples" / "six_calls.json", 0, 0)
    _provider_fixture(episode)
    metrics = json.loads((episode / "metrics.json").read_text())
    attempts_path = episode / "api_attempts.jsonl"
    attempts = [json.loads(line) for line in attempts_path.read_text().splitlines()]
    response = attempts[-1]
    old = response["usage_raw"]["total_tokens"]
    response["usage_raw"]["prompt_tokens"] += metrics["max_tokens"]
    response["usage_raw"]["total_tokens"] += metrics["max_tokens"]
    response.update(account_usage(response["usage_raw"]), response_delivered=False,
                    budget_error="token budget exceeded")
    delta = response["usage_raw"]["total_tokens"] - old
    metrics["input_tokens"] += delta
    metrics["total_tokens"] += delta
    metrics["provider"]["reported_total_tokens"] += delta
    metrics["token_cap_certified"] = False
    metrics["termination_reason"] = "budget_exhausted"
    trajectory = episode / "trajectory.jsonl"
    rows = [json.loads(line) for line in trajectory.read_text().splitlines()]
    del rows[max(index for index, row in enumerate(rows) if row["kind"] == "model")]
    if tamper == "missing-prior-response":
        del rows[next(index for index, row in enumerate(rows) if row["kind"] == "model")]
    elif tamper == "false-budget-error":
        del response["budget_error"]
    attempts_path.write_text("".join(json.dumps(event) + "\n" for event in attempts))
    trajectory.write_text("".join(json.dumps(row) + "\n" for row in rows))
    write_json(episode / "provider_metrics.json", metrics["provider"])
    if tamper:
        with pytest.raises(SubmissionError, match="missing model|budget/error evidence"):
            submission._check_provider(episode, metrics)
    else:
        submission._check_provider(episode, metrics)


@pytest.mark.parametrize("status", ["patch_export_or_execution_failed", "cleanup_pending"])
@pytest.mark.parametrize("path_kind", ["absolute", "relative"])
def test_recovery_metadata_packages_without_following_worktree(tmp_path, status, path_kind):
    import os

    results = _required_results(tmp_path)
    episode = tmp_path / "evidence" / EPISODE
    worktree = tmp_path / "retained-scratch" / "worktree"
    worktree.mkdir(parents=True)
    sentinel = b"SCRATCH_CONTENT_MUST_NEVER_ENTER_THE_SUBMISSION\n"
    (worktree / "scratch_only.py").write_bytes(sentinel)
    recovery_path = episode / "RECOVERY.json"
    recovery = {
        "status": status,
        "worktree": str(worktree) if path_kind == "absolute" else os.path.relpath(worktree, episode),
        "recovery": "Preserve this scratch directory for manual inspection.",
    }
    write_json(recovery_path, recovery)

    # Exercise the filename registry before the existing manifest knows about
    # the newly exported metadata. Path values are never copy instructions.
    copied = tmp_path / "copied-evidence"
    submission._copy_evidence(episode, copied)
    assert (copied / "RECOVERY.json").read_bytes() == recovery_path.read_bytes()
    assert not (copied / "scratch_only.py").exists()

    if status == "patch_export_or_execution_failed":
        metrics_path = episode / "metrics.json"
        state_path = episode / "final_state.json"
        metrics = json.loads(metrics_path.read_text())
        state = json.loads(state_path.read_text())
        metrics.update(termination_reason="error", patch_nonempty=False, resolved=False)
        state["status"] = "error"
        trajectory = episode / "trajectory.jsonl"
        rows = [json.loads(line) for line in trajectory.read_text().splitlines()]
        budget_keys = next(row["payload"] for row in rows if row["kind"] == "budget")
        rows.extend([
            {"kind": "budget", "payload": {key: metrics[key] for key in budget_keys}},
            {"kind": "error", "payload": {
                "step": state["step"], "phase": "execution_or_patch_export",
                "message": "Offline fixture: patch export failed", "agent_state_available": True,
            }},
            {"kind": "state", "payload": {"step": state["step"], "status": "error"}},
        ])
        trajectory.write_text("".join(json.dumps(row) + "\n" for row in rows))
        (episode / "patch.diff").write_text("")
        write_json(metrics_path, metrics)
        write_json(state_path, state)
        write_json(episode / "verification.json", validate_patch(
            ROOT / "tasks" / "dev" / TASK_NAME, episode / "patch.diff"))

    _refresh_all(tmp_path / "evidence")
    assert _package(tmp_path, results)["valid"] is True
    assert check_submission(tmp_path / "submission.zip")["valid"] is True
    with zipfile.ZipFile(tmp_path / "submission.zip") as archive:
        prefix = f"evidence/{EPISODE}"
        assert archive.read(f"{prefix}/RECOVERY.json") == recovery_path.read_bytes()
        manifest = json.loads(archive.read(f"{prefix}/episode_manifest.json"))
        assert manifest["files"]["RECOVERY.json"] == sha256(recovery_path)
        record = json.loads(archive.read("evidence/baseline/summary.json"))["records"][0]
        if status == "patch_export_or_execution_failed":
            assert record["outcome"] == "error" and record["resolved"] is False
            assert record["runner_returncode"] == 1
        assert all("worktree" not in Path(name).parts for name in archive.namelist())
        assert all("scratch_only.py" not in name for name in archive.namelist())
        assert all(sentinel not in archive.read(name) for name in archive.namelist())
    assert (worktree / "scratch_only.py").read_bytes() == sentinel


def _recovery_plan(tmp_path):
    results = _required_results(tmp_path)
    labels = ("z_full", "a_greedy", "m_strong")
    roots = {label: results[1][1] / "conditions" / label for label in labels}
    # Simulate an interrupted original slot. Its partial bytes remain history.
    for episode in (results[0][1] / TASK_NAME, roots["a_greedy"] / TASK_NAME):
        (episode / "final_state.json").unlink()
        with (episode / "api_attempts.jsonl").open("a") as handle:
            handle.write('{"interrupted_mid_record":')
        write_json(episode / "INCOMPLETE.json", {"status": "interrupted"})
    replacements = {}
    for label, config in zip(labels, ("default.json", "examples/no_context.json", "examples/no_feedback.json")):
        root = tmp_path / "retry" / label
        episode = root / TASK_NAME
        _write_episode(ROOT / "tasks/dev" / TASK_NAME, episode, ROOT / "config" / config, 0, 2)
        _provider_fixture(episode)
        _bind_episode(episode)
        replacements[label] = str(root)
    naive = tmp_path / "naive-retry"
    _write_episode(ROOT / "tasks/dev" / TASK_NAME, naive / TASK_NAME,
                   ROOT / "config/examples/six_calls.json", 0, 0)
    _provider_fixture(naive / TASK_NAME)
    _bind_episode(naive / TASK_NAME)
    plan = {
        "format_version": 1, "baseline": str(results[0][1]),
        "conditions": {label: str(root) for label, root in roots.items()},
        "condition_order": list(labels),
        "replacements": [{"tasks": [TASK_NAME], "conditions": replacements, "reason": "provider outage"}],
        "baseline_replacements": [{"tasks": [TASK_NAME], "root": str(naive), "reason": "interrupted baseline"}],
        "budget_variable": False,
    }
    path = tmp_path / "recovery.json"
    write_json(path, plan)
    return path, plan


def _package_study(tmp_path, study, supplements=None):
    report, disclosure, reproduction = _completed_documents(tmp_path)
    return package_submission(
        source_root=ROOT, output_path=tmp_path / "submission.zip", report_path=report,
        disclosure_path=disclosure, reproduction_path=reproduction, results=[], includes=[],
        study=study, supplements=supplements or [],
    )


def test_assemble_interrupted_slots_preserves_all_receipts_and_unscored_probes(tmp_path):
    plan_path, plan = _recovery_plan(tmp_path)
    original = {str(path): path.read_bytes() for folder in ("evidence", "retry", "naive-retry")
                for path in (tmp_path / folder).rglob("*") if path.is_file()}
    study = tmp_path / "effective"
    result = submission.assemble_study(plan_path, study)
    assert result["effective_episodes"] == 48 and result["model_calls"] == 0
    assert result["retained_roots"] == 8
    assert all(Path(path).read_bytes() == data for path, data in original.items())
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "api_attempts.jsonl").write_text('{"partial_request":')
    write_json(probe / "metrics.json", {"resolved": True, "tasks": 9000})
    payload = _package_study(tmp_path, study, [("probe", probe)])
    assert payload["valid"] and payload["metrics_files"] == 48 and payload["supplement_files"] > 0
    assert check_submission(tmp_path / "submission.zip")["valid"]
    with zipfile.ZipFile(tmp_path / "submission.zip") as archive:
        ledger = json.loads(archive.read("selection_ledger.json"))
        assert ledger["condition_order"] == ["z_full", "a_greedy", "m_strong"]
        assert ledger["selected"][f"a_greedy/{TASK_NAME}"]["source"] == "paired-replacement-001-a_greedy"
        original_metrics = Path(plan["conditions"]["z_full"]) / "csv_records/metrics.json"
        assert archive.read("evidence/paired/conditions/z_full/csv_records/metrics.json") == original_metrics.read_bytes()
        replacement_metrics = Path(plan["replacements"][0]["conditions"]["a_greedy"]) / TASK_NAME / "metrics.json"
        assert archive.read(f"evidence/paired/conditions/a_greedy/{TASK_NAME}/metrics.json") == replacement_metrics.read_bytes()
        assert archive.read(f"supplements/original-a_greedy/{TASK_NAME}/api_attempts.jsonl").endswith(
            b'{"interrupted_mid_record":')
        assert archive.read("supplements/probe/api_attempts.jsonl") == b'{"partial_request":'
        assert json.loads(archive.read("evidence/baseline/summary.json"))["tasks"] == 12
        comparisons = json.loads(archive.read("evidence/paired/paired_summary.json"))["comparisons_to_first_condition"]
        assert all(item["baseline"] == "z_full" for item in comparisons.values())
        assert not any("worktree" in Path(name).parts or "repo" in Path(name).parts for name in archive.namelist())


@pytest.mark.parametrize("change", ["patch-upgrade", "python-minor", "pytest-version"])
def test_recovery_runtime_compatibility_preserves_complete_receipts(tmp_path, change):
    plan_path, plan = _recovery_plan(tmp_path)
    roots = [*plan["replacements"][0]["conditions"].values(),
             plan["baseline_replacements"][0]["root"]]
    preserved = {}
    for index, root in enumerate(roots):
        episode = Path(root) / TASK_NAME
        path = episode / "metrics.json"
        metrics = json.loads(path.read_text())
        runtime = metrics["run"]["runtime"]
        runtime.update(
            python=".".join(runtime["python"].split(".")[:2]) + f".{90 + index} (patch update)",
            executable_sha256=str(index + 1) * 64,
            platform=runtime["platform"].split("-", 1)[0] + f"-updated-release-{index}",
        )
        if index == 0 and change == "python-minor":
            runtime["python"] = "99.1.0"
        elif index == 0 and change == "pytest-version":
            runtime["dependencies"] = {**runtime["dependencies"], "pytest": "99.1"}
        write_json(path, metrics)
        _bind_episode(episode)
        preserved[str(path)] = path.read_bytes()

    study = tmp_path / "effective"
    if change != "patch-upgrade":
        with pytest.raises(SubmissionError, match="identity|runtime|settings"):
            submission.assemble_study(plan_path, study)
        assert not study.exists()
        return
    assembled = submission.assemble_study(plan_path, study)
    assert assembled["effective_episodes"] == 48 and assembled["model_calls"] == 0
    assert _package_study(tmp_path, study)["valid"]
    assert check_submission(tmp_path / "submission.zip")["valid"]
    assert all(Path(path).read_bytes() == content for path, content in preserved.items())
    with zipfile.ZipFile(tmp_path / "submission.zip") as archive:
        for label, root in plan["replacements"][0]["conditions"].items():
            name = f"evidence/paired/conditions/{label}/{TASK_NAME}/metrics.json"
            assert archive.read(name) == preserved[str(Path(root) / TASK_NAME / "metrics.json")]
        assert archive.read(f"evidence/baseline/{TASK_NAME}/metrics.json") == preserved[
            str(Path(plan["baseline_replacements"][0]["root"]) / TASK_NAME / "metrics.json")]


@pytest.mark.parametrize("tamper", [
    "missing-condition", "missing-selected-file", "same-output", "unknown-task", "changed-runtime",
    "superseded-incomplete-block", "unsafe-source-identity", "cross-original", "cross-replacement",
])
def test_assemble_rejects_incomplete_or_unpaired_replacements(tmp_path, tamper):
    plan_path, plan = _recovery_plan(tmp_path)
    if tamper == "missing-condition":
        del plan["replacements"][0]["conditions"]["a_greedy"]
    elif tamper == "missing-selected-file":
        (Path(plan["replacements"][0]["conditions"]["a_greedy"]) / TASK_NAME / "metrics.json").unlink()
    elif tamper == "same-output":
        plan["replacements"][0]["conditions"]["z_full"] = plan["conditions"]["z_full"]
    elif tamper == "changed-runtime":
        episode = Path(plan["replacements"][0]["conditions"]["z_full"]) / TASK_NAME
        _edit(episode / "metrics.json", "run.runtime.python", "different-runtime")
        _bind_episode(episode)
    elif tamper == "superseded-incomplete-block":
        later = copy.deepcopy(plan["replacements"][0])
        for label, root in later["conditions"].items():
            target = tmp_path / "retry-again" / label
            shutil.copytree(root, target)
            later["conditions"][label] = str(target)
        plan["replacements"].append(later)
        (Path(plan["replacements"][0]["conditions"]["a_greedy"]) / TASK_NAME / "metrics.json").unlink()
    elif tamper == "unsafe-source-identity":
        episode = Path(plan["replacements"][0]["conditions"]["z_full"]) / TASK_NAME
        _edit(episode / "metrics.json", "run.source_sha256", str(ROOT))
        _bind_episode(episode)
    elif tamper == "cross-original":
        plan["conditions"]["a_greedy"] = str(Path(plan["conditions"]["z_full"]) / "../z_full")
    elif tamper == "cross-replacement":
        plan["replacements"][0]["conditions"]["a_greedy"] = plan["replacements"][0]["conditions"]["z_full"]
    else:
        plan["replacements"][0]["tasks"] = ["not-a-public-task"]
    write_json(plan_path, plan)
    with pytest.raises(SubmissionError, match="all three|metrics.json|new output|public task|identity"):
        submission.assemble_study(plan_path, tmp_path / "effective")
    assert not (tmp_path / "effective").exists()


def test_effective_receipt_cannot_be_rewritten_even_with_rehashed_envelopes(tmp_path):
    plan_path, _ = _recovery_plan(tmp_path)
    study = tmp_path / "effective"
    submission.assemble_study(plan_path, study)
    episode = study / "evidence/paired/conditions/a_greedy" / TASK_NAME
    with (episode / "metrics.json").open("a") as handle:
        handle.write("\n")
    _rehash_envelopes(study / "evidence")
    with pytest.raises(SubmissionError, match="differs from retained original"):
        _package_study(tmp_path, study)


@pytest.mark.parametrize("tamper", [
    "missing-ledger", "missing-condition", "receipt-hash", "missing-original", "scored-supplement",
    "cross-condition-root",
])
def test_archive_checker_enforces_ledger_and_history_after_outer_rehash(tmp_path, tamper):
    plan_path, _ = _recovery_plan(tmp_path)
    study = tmp_path / "effective"
    submission.assemble_study(plan_path, study)
    _package_study(tmp_path, study)
    archive = tmp_path / "submission.zip"
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extracted)
    ledger = extracted / "selection_ledger.json"
    if tamper == "missing-ledger":
        ledger.unlink()
    elif tamper == "missing-condition":
        _edit(ledger, "blocks.1.sources.a_greedy", DELETE)
    elif tamper == "receipt-hash":
        _edit(ledger, f"selected.a_greedy/{TASK_NAME}.receipt_sha256", "0" * 64)
    elif tamper == "missing-original":
        (extracted / "supplements/original-a_greedy" / TASK_NAME / "api_attempts.jsonl").unlink()
    elif tamper == "scored-supplement":
        _edit(extracted / "supplements_manifest.json", "supplements.original-a_greedy.use", "counts as resolved")
    else:
        index = json.loads((extracted / "supplements_manifest.json").read_text())
        _edit(extracted / "supplements_manifest.json", "supplements.original-a_greedy.source",
              index["supplements"]["original-z_full"]["source"])
    _rewrite_archive(extracted, archive)
    with pytest.raises(SubmissionError, match="selection ledger|all three|unindexed|excluded from aggregate|globally unique"):
        check_submission(archive)


def test_assembled_failed_replacement_remains_error_not_success(tmp_path):
    plan_path, plan = _recovery_plan(tmp_path)
    episode = Path(plan["replacements"][0]["conditions"]["a_greedy"]) / TASK_NAME
    metrics = json.loads((episode / "metrics.json").read_text())
    state = json.loads((episode / "final_state.json").read_text())
    metrics.update(termination_reason="error", resolved=False, patch_nonempty=False)
    state["status"] = "error"
    write_json(episode / "metrics.json", metrics)
    write_json(episode / "final_state.json", state)
    with (episode / "trajectory.jsonl").open("a") as handle:
        handle.write(json.dumps({"kind": "error", "payload": {
            "step": state["step"], "phase": "execution_or_patch_export", "message": "export failed",
            "agent_state_available": True,
        }}) + "\n")
        handle.write(json.dumps({"kind": "state", "payload": {"step": state["step"], "status": "error"}}) + "\n")
    (episode / "patch.diff").write_text("")
    write_json(episode / "RECOVERY.json", {"status": "patch_export_or_execution_failed",
                                         "scratch": "/outside/output/worktree-never-copied"})
    write_json(episode / "verification.json", validate_patch(ROOT / "tasks/dev" / TASK_NAME, episode / "patch.diff"))
    _bind_episode(episode)
    study = tmp_path / "effective"
    submission.assemble_study(plan_path, study)
    assert _package_study(tmp_path, study)["valid"]
    summary = json.loads((study / "evidence/paired/conditions/a_greedy/summary.json").read_text())
    record = next(record for record in summary["records"] if record["task_name"] == TASK_NAME)
    assert record["outcome"] == "error" and record["resolved"] is False
    assert record["runner_returncode"] == 1 and summary["runner_failures"] == 1
    assert (study / "evidence/paired/conditions/a_greedy" / TASK_NAME / "RECOVERY.json").read_bytes() == (
        episode / "RECOVERY.json").read_bytes()


@pytest.mark.parametrize("kind", ["secret", "symlink", "oversize", "source-code"])
def test_supplement_strict_boundary(tmp_path, monkeypatch, kind):
    source = tmp_path / "probe"
    source.mkdir()
    path = source / "request.log"
    path.write_text('{"partial":')
    if kind == "secret":
        path.write_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(ROOT / "requirements.lock")
    elif kind == "oversize":
        monkeypatch.setattr(submission, "MAX_FILE_BYTES", 4)
    else:
        (source / "task_source.py").write_text("private_task_source = True\n")
    with pytest.raises(SubmissionError, match="credential|symlink|exceeds|not a task repository"):
        submission._copy_supplement(source, tmp_path / "copied")


def test_supplement_records_explicit_worktree_exclusion(tmp_path):
    source = tmp_path / "partial"
    (source / "worktree").mkdir(parents=True)
    (source / "worktree" / "private_task.py").write_text("must_not_copy = True")
    (source / "api_attempts.jsonl").write_text('{"interrupted":')
    copied = tmp_path / "copied"
    metadata = submission._copy_supplement(source, copied)
    assert metadata["excluded"] == ["worktree/"]
    assert (copied / "api_attempts.jsonl").read_bytes() == (source / "api_attempts.jsonl").read_bytes()
    assert not (copied / "worktree").exists()


def test_equal_lower_family_caps_are_allowed_and_varying_caps_need_declaration(tmp_path):
    source = tmp_path / "source"
    for relative in submission.REQUIRED_RUNTIME_PATHS:
        path = ROOT / relative
        if path.is_dir():
            shutil.copytree(path, source / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, source / relative)
    for name in ("default.json", "examples/no_context.json", "examples/no_feedback.json"):
        _edit(source / "config" / name, "max_model_calls", 6)
    results = _required_results(tmp_path, source)
    assert _package(tmp_path, results, source_root=source)["valid"]
    _edit(source / "config/examples/no_context.json", "max_model_calls", 8)
    condition = results[1][1] / "conditions/a_greedy"
    shutil.rmtree(condition)
    for index, task in enumerate(discover_tasks(ROOT / "tasks/dev")):
        _write_episode(task, condition / task.name, source / "config/examples/no_context.json", index, 1)
    _refresh_paired(results[1][1])
    assert _package(tmp_path, results, source_root=source)["valid"]
    _edit(results[1][1] / "paired_summary.json", "budget_variable", False)
    with pytest.raises(SubmissionError, match="equal family budgets"):
        _package(tmp_path, results, source_root=source)


@pytest.mark.parametrize("kind", ["namespace", "reserved", "module", "package"])
def test_python_includes_use_runtime_source_inventory(tmp_path, kind):
    root = tmp_path / "source"
    root.mkdir()
    if kind == "module":
        include = root / "mechanism.py"
        include.write_text("def choose(): return 'inspect'\n")
    else:
        include = root / ("outputs" if kind == "reserved" else "mechanism")
        include.mkdir()
        (include / "policy.py").write_text("def choose(): return 'inspect'\n")
        if kind != "namespace":
            (include / "__init__.py").write_text("")
    if kind in {"module", "package"}:
        submission._check_python_include(root, include)
        assert any(name.startswith("mechanism") for name in source_files(root))
    else:
        with pytest.raises(SubmissionError, match="hash-backed.*__init__"):
            submission._check_python_include(root, include)


def test_evidence_copy_preserves_hash_backed_custom_source_snapshots(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "strategy.py").write_text("VALUE = 'original'\n")
    (original / "mechanism").mkdir()
    (original / "mechanism/__init__.py").write_text("")
    (original / "mechanism/policy.py").write_text("VALUE = 'original'\n")
    files = source_files(original)
    identity = {"source_files": files, "source_sha256": submission.object_sha256(files)}
    batch = tmp_path / "batch"
    episode = batch / TASK_NAME
    episode.mkdir(parents=True)
    snapshot_source(batch, identity, original)
    write_episode_manifest(episode, identity, 0, "")
    (original / "strategy.py").write_text("VALUE = 'changed final source'\n")
    destination = tmp_path / "copied"
    submission._copy_evidence(batch, destination)
    snapshot = destination / "sources" / identity["source_sha256"]
    assert source_files(snapshot) == files
    assert (snapshot / "strategy.py").read_text() == "VALUE = 'original'\n"
    assert (snapshot / "mechanism/policy.py").read_text() == "VALUE = 'original'\n"


def test_custom_modules_package_with_original_snapshots_and_changed_final_source(tmp_path):
    source = tmp_path / "source"
    for relative in submission.REQUIRED_RUNTIME_PATHS:
        submission._copy_source_path(ROOT, ROOT / relative, source / relative)
    (source / "strategy.py").write_text("VALUE = 'original'\n")
    (source / "mechanism").mkdir()
    (source / "mechanism/__init__.py").write_text("")
    (source / "mechanism/policy.py").write_text("VALUE = 'original'\n")
    results = _required_results(tmp_path, source)
    for batch in (results[1][1] / "conditions").iterdir():
        identity = json.loads((batch / TASK_NAME / "metrics.json").read_text())["run"]
        snapshot_source(batch, identity, source)
    (source / "strategy.py").write_text("VALUE = 'changed final source'\n")
    report, disclosure, reproduce = _completed_documents(tmp_path)
    result = package_submission(
        source_root=source, output_path=tmp_path / "submission.zip", results=results,
        report_path=report, disclosure_path=disclosure, reproduction_path=reproduce,
        includes=[source / "strategy.py", source / "mechanism"],
    )
    assert result["valid"] and result["metrics_files"] == 48
    with zipfile.ZipFile(tmp_path / "submission.zip") as archive:
        assert archive.read("strategy.py") == b"VALUE = 'changed final source'\n"
        assert archive.read(
            f"evidence/paired/conditions/m_strong/sources/{identity['source_sha256']}/strategy.py"
        ) == b"VALUE = 'original'\n"
        assert "mechanism/__init__.py" in archive.namelist()
