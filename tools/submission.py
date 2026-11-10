"""Build and validate a deterministic HW2 submission archive."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import statistics
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
# Direct ``python tools/submission.py check ...`` uses the checker release's
# evaluator and public bank, never Python modules from the submitted archive.
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from eval.report import (TASK_COLUMNS, budget_compliant, wilson_interval, write_json,
                         verified_resolution, load_task_record, write_batch_reports, write_paired_reports)
from eval.run_dev import FINAL_STATE_FIELDS, discover_tasks, validate_episode_manifest
from eval.run_identity import task_input_sha256, source_files, object_sha256, comparison_identity, execution_identity, runtime_comparison_identity, SOURCE_PATHS
from eval.validate_patch import _test_report_error, changed_paths, validate_patch


INTEGRITY_MANIFEST = Path(__file__).with_name("release_integrity.json")
EPISODE_FILES = (
    "patch.diff", "trajectory.jsonl", "metrics.json", "final_state.json", "verification.json",
    "provider_metrics.json", "api_attempts.jsonl", "episode_manifest.json",
)
COST_FIELDS = (
    "model_calls", "input_tokens", "output_tokens", "total_tokens",
    "active_seconds", "test_invocations", "test_seconds",
)
INTEGER_METRICS = (
    "model_calls", "input_tokens", "output_tokens", "total_tokens", "test_invocations",
    "max_model_calls", "max_tokens",
)
BUDGET_LIMITS = {
    "max_model_calls": ("max_model_calls", 12),
    "max_tokens": ("max_total_tokens", 120000),
    "max_active_seconds": ("max_active_seconds", 1800.0),
}
REQUIRED_RUNTIME_PATHS = (
    "run_agent.py",
    "harness",
    "eval",
    "config",
    "requirements.lock",
    "pyproject.toml",
)
REQUIRED_PACKAGE_PATHS = (
    "run_agent.py",
    "harness/budget.py",
    "harness/model_client.py",
    "config/default.json",
    "requirements.lock",
    "ai_disclosure.md",
    "REPRODUCE.md",
    "submission_manifest.json",
)
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    ".pilot-output",
    "grader_tests",
    ".grader_tests",
    "instructor",
    "tasks",
    "tests",
    "worktree",
}
FORBIDDEN_FILENAMES = {
    ".env",
    ".env.local",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
FORBIDDEN_CLAIM_KEYS = {
    "final_hidden_score",
    "hidden_score",
    "private_score",
    "private_validation_score",
}
EVIDENCE_FILENAMES = {
    "patch.diff",
    "trajectory.jsonl",
    "metrics.json",
    "final_state.json",
    "verification.json",
    "provider_metrics.json",
    "api_attempts.jsonl",
    "episode_manifest.json",
    "INCOMPLETE.json",
    "RECOVERY.json",
    "summary.json",
    "tasks.csv",
    "failure_index.md",
    "paired_summary.json",
    "paired_tasks.csv",
    "paired_summary.md",
}
SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|secret|password|passwd|access[_-]?token)"
        r"[\"']?\s*[:=]\s*[\"']([^\"'\n]{8,})[\"']"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:nvapi-|sk-(?:proj-)?)[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\bBearer\s+(?!\[REDACTED\]|local-development-only)[A-Za-z0-9._~+/-]{12,}"),
)
PLACEHOLDER_PATTERNS = (
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFILL (?:THIS|ME) IN\b", re.IGNORECASE),
    re.compile(r"\bREPLACE THIS TEMPLATE\b", re.IGNORECASE),
    re.compile(r"\bWORKING DRAFT\b", re.IGNORECASE),
)
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_FILES = 20_000
SUPPLEMENT_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".md", ".csv", ".diff", ".patch",
                       ".toml", ".lock"}
SUPPLEMENT_OMIT_DIRS = FORBIDDEN_PARTS | {"repo"}
LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class SubmissionError(RuntimeError):
    pass


def package_submission(
    *,
    source_root: Path,
    output_path: Path,
    report_path: Path,
    disclosure_path: Path,
    reproduction_path: Path,
    results: list[tuple[str, Path]],
    includes: list[Path],
    supplements: list[tuple[str, Path]] | None = None,
    study: Path | None = None,
) -> dict:
    _require_file(report_path, "report")
    if report_path.suffix.lower() not in {".pdf", ".md"}:
        raise SubmissionError("report must be a PDF or Markdown file")
    if report_path.suffix.lower() == ".md":
        _require_completed_text(report_path, "report")
    elif report_path.stat().st_size < 100:
        raise SubmissionError("report PDF is incomplete")
    _require_completed_text(disclosure_path, "AI disclosure")
    _require_completed_text(reproduction_path, "reproduction instructions")
    if study is not None and results:
        raise SubmissionError("use either --study or --results, not both")
    if not results and study is None:
        raise SubmissionError("at least one --results directory is required")
    labels = [label for label, _ in results]
    if len(labels) != len(set(labels)):
        raise SubmissionError("result labels must be unique")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", label) for label in labels):
        raise SubmissionError("invalid result label")

    with tempfile.TemporaryDirectory(prefix="hw2-package-") as temp:
        stage = Path(temp) / "submission"
        stage.mkdir()
        for relative in REQUIRED_RUNTIME_PATHS:
            source = source_root / relative
            if not source.exists():
                raise SubmissionError(f"missing runtime path: {relative}")
            _copy_source_path(source_root, source, stage / relative)
        if (source_root / "Dockerfile").is_file():
            _copy_source_path(source_root, source_root / "Dockerfile", stage / "Dockerfile")
        for include in includes:
            if include.is_symlink():
                raise SubmissionError("extra include must not be a symlink")
            include = include.resolve()
            try:
                relative = include.relative_to(source_root)
            except ValueError as error:
                raise SubmissionError(f"extra include is outside source root: {include}") from error
            _check_python_include(source_root, include)
            _copy_source_path(source_root, include, stage / relative)

        shutil.copy2(disclosure_path, stage / "ai_disclosure.md")
        shutil.copy2(reproduction_path, stage / "REPRODUCE.md")
        shutil.copy2(report_path, stage / f"report{report_path.suffix.lower()}")
        evidence_root = stage / "evidence"
        supplemental = {}
        if study is not None:
            supplemental = _stage_study(study.resolve(), stage)
        else:
            for label, result_root in results:
                _copy_evidence(result_root, evidence_root / label)
        for label, directory in supplements or []:
            _require_label(label)
            if label in supplemental:
                raise SubmissionError(f"duplicate supplement label: {label}")
            supplemental[label] = _copy_supplement(directory, stage / "supplements" / label)
            _check_file_limits(stage)
        if supplemental:
            write_json(stage / "supplements_manifest.json",
                       {"format_version": 1, "supplements": supplemental})

        manifest = {
            "format_version": 1,
            "files": _file_hashes(stage, exclude={"submission_manifest.json"}),
            "result_labels": [label for label, _ in results],
            "evidence_mode": "assembled" if study is not None else "results",
            "claims": {
                "contains_hidden_score": False,
                "contains_private_or_final_tasks": False,
            },
        }
        (stage / "submission_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _scan_for_secrets(stage)
        _check_file_limits(stage)
        candidate = Path(temp) / "candidate.zip"
        _write_deterministic_zip(stage, candidate)
        result = check_submission(candidate)
        shutil.copyfile(candidate, output_path)

    return {**result, "archive": str(output_path)}


def check_submission(archive_path: Path) -> dict:
    _require_file(archive_path, "submission archive")
    if not zipfile.is_zipfile(archive_path):
        raise SubmissionError("submission must be a valid ZIP archive")
    with tempfile.TemporaryDirectory(prefix="hw2-check-") as temp:
        extracted = Path(temp) / "submission"
        extracted.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            _validate_zip_members(archive.infolist())
            archive.extractall(extracted)

        files = sorted(
            path.relative_to(extracted).as_posix()
            for path in extracted.rglob("*")
            if path.is_file()
        )
        missing = [path for path in REQUIRED_PACKAGE_PATHS if path not in files]
        if missing:
            raise SubmissionError(f"missing required package files: {missing}")
        if not any(path in files for path in ("report.pdf", "report.md")):
            raise SubmissionError("submission is missing report.pdf or report.md")
        _reject_forbidden_paths(files)
        _check_integrity(extracted)
        _check_manifest(extracted)
        _scan_for_secrets(extracted)
        _reject_hidden_score_claims(extracted)
        supplemental = _check_supplements(extracted)
        _require_completed_text(extracted / "ai_disclosure.md", "AI disclosure")
        _require_completed_text(extracted / "REPRODUCE.md", "reproduction instructions")

        metrics = [
            path for path in files
            if path.startswith("evidence/") and path.endswith("/metrics.json")
        ]
        summaries = [
            path for path in files
            if path.startswith("evidence/") and path.endswith("/summary.json")
        ]
        paired_summaries = [
            path for path in files
            if path.startswith("evidence/") and path.endswith("/paired_summary.json")
        ]
        if not metrics:
            raise SubmissionError("evidence must include at least one metrics.json")
        if not summaries:
            raise SubmissionError("evidence must include a public-dev summary.json")
        _check_required_experiment_evidence(
            extracted,
            summaries=summaries,
            paired_summaries=paired_summaries,
        )
        manifest = _load_json_object(extracted / "submission_manifest.json", "submission manifest")
        if manifest.get("evidence_mode") == "assembled" or (extracted / "selection_ledger.json").exists():
            _check_selection_ledger(extracted)

    return {
        "valid": True,
        "archive": str(archive_path),
        "files": len(files),
        "metrics_files": len(metrics),
        "summary_files": len(summaries) + len(paired_summaries),
        "supplement_files": sum(len(item["files"]) for item in supplemental.values()),
        "verification": "evidence consistency; use reverify for independent local patch checks",
    }


def reverify_submission(archive_path: Path, output_root: Path) -> dict:
    """Run only the trusted local verifier against patches and the staff task bank."""
    check_submission(archive_path)
    if output_root.exists() and any(output_root.iterdir()):
        raise SubmissionError("reverification output must be a new or empty directory")
    output_root.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix="hw2-reverify-") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        for patch in sorted((extracted / "evidence").rglob("patch.diff")):
            relative = patch.parent.relative_to(extracted / "evidence")
            task = ROOT / "tasks" / "dev" / patch.parent.name
            result = validate_patch(task, patch)
            result.update(patch_sha256=_sha256(patch), task_input_sha256=task_input_sha256(task))
            destination = output_root / relative
            destination.mkdir(parents=True, exist_ok=True)
            write_json(destination / "verification.json", result)
            original = _load_json_object(patch.with_name("verification.json"), str(patch))
            records.append({
                "episode": relative.as_posix(),
                "submitted_resolved": original["resolved"],
                "fresh_resolved": result["resolved"],
                "verdict_matches": all(original[field] == result[field] for field in
                    ("resolved", "patch_valid", "f2p_passed", "p2p_passed", "protected_paths_clean")),
            })
    summary = {
        "episodes": len(records), "records": records, "model_calls": 0,
        "verdict_mismatches": sum(not record["verdict_matches"] for record in records),
    }
    write_json(output_root / "reverification_summary.json", summary)
    return summary


def _check_required_experiment_evidence(
    root: Path,
    *,
    summaries: list[str],
    paired_summaries: list[str],
) -> None:
    """Check complete, internally consistent public evidence, not model authenticity.

    The fingerprints bind the declared inputs to this release and packaged configs.
    They cannot attest that a student actually executed that model or harness.
    Staff can independently reverify patches without running student model code.
    """
    tasks = discover_tasks(ROOT / "tasks" / "dev")
    public = {}
    for task in tasks:
        metadata = _load_json_object(task / "task.json", str(task))
        public[metadata["id"]] = {
            "name": task.name,
            "issue": metadata["issue"],
            "sha256": task_input_sha256(task),
            "f2p": metadata["fail_to_pass"],
            "p2p": metadata["pass_to_pass"],
        }
    if len(public) != 12:
        raise SubmissionError("checker release must contain exactly 12 public-dev tasks")
    configs = {
        _sha256(path): path
        for path in root.rglob("*.json")
        if path.relative_to(root).parts[0] != "evidence"
        and path.name != "submission_manifest.json"
    }
    naive_digest = _sha256(ROOT / "config" / "examples" / "six_calls.json")
    integrity = _load_json_object(INTEGRITY_MANIFEST, "release integrity")
    batches = {}
    source_cache = {}
    expected_episodes = set()
    for relative in summaries:
        path = root / relative
        batch, identity = _check_batch(path, public, configs, root, source_cache)
        batches[path.parent] = (batch, identity)
        expected_episodes.update(
            path.parent / record["task_name"] for record in batch["records"]
        )
    baselines = [
        item for item in batches.values()
        if item[0]["split"] == "public-dev" and item[1]["config_sha256"] == naive_digest
    ]
    if not baselines:
        raise SubmissionError(
            "evidence must include a complete 12-task public-dev Naive baseline "
            "using config/examples/six_calls.json"
        )
    naive = integrity.get("naive_sha256")
    if not isinstance(naive, dict) or not naive:
        raise SubmissionError("checker release is missing Naive source provenance hashes")
    for _, identity in baselines:
        if identity["source_files"] != naive:
            raise SubmissionError("Naive baseline must use the unmodified original runtime source")
    if not paired_summaries:
        raise SubmissionError(
            "evidence must include baseline plus at least two paired ablations "
            "over all 12 public-dev tasks"
        )
    for relative in paired_summaries:
        _check_paired(root / relative, batches, baselines)
    actual_episodes = {
        path.parent
        for path in (root / "evidence").rglob("*")
        if path.is_file() and path.name in EPISODE_FILES
    }
    if actual_episodes != expected_episodes:
        raise SubmissionError(
            "episode evidence must belong to a complete public-dev summary (orphan artifacts)"
        )


def _check_batch(
    path: Path, public: dict, configs: dict[str, Path], root: Path, source_cache: dict,
) -> tuple[dict, dict]:
    batch = _load_json_object(path, str(path))
    split = batch.get("split")
    if not isinstance(split, str) or not (
        split == "public-dev" or split.startswith("public-dev:")
    ):
        raise SubmissionError(f"{path}: expected a public-dev split")
    records = batch.get("records")
    if not isinstance(records, list) or len(records) != 12:
        raise SubmissionError(f"{path}: records must cover all 12 public-dev tasks")
    ids = [record.get("task_id") if isinstance(record, dict) else None for record in records]
    if any(not isinstance(task_id, str) for task_id in ids) or set(ids) != set(public):
        raise SubmissionError(f"{path}: records must use the actual 12 public-dev task IDs")
    identities = []
    for record in records:
        task = public[record["task_id"]]
        _expect(record.get("task_name"), task["name"], f"{path}: task_name")
        episode = path.parent / task["name"]
        metrics = _check_episode(episode, task, configs, root, source_cache)
        identity = {key: value for key, value in metrics["run"].items()
                    if key != "task_input_sha256"}
        if identities:
            _expect(execution_identity(identity), execution_identity(identities[0]),
                    f"{path}: seed/config/source/model/runtime compatibility")
        identities.append(identity)
        verification = _load_json_object(episode / "verification.json", str(episode))
        error = record.get("error")
        manifest = _load_json_object(episode / "episode_manifest.json", str(episode))
        _expect(record.get("runner_returncode"), manifest["runner_returncode"], f"{path}: runner_returncode")
        _expect(error, manifest["error"], f"{path}: error")
        failure = bool(manifest["runner_returncode"] or error or metrics["termination_reason"] in {"error", "interrupted"})
        resolved = bool(
            verified_resolution(verification, {"fail_to_pass": task["f2p"], "pass_to_pass": task["p2p"]},
                                episode / "patch.diff")
            and budget_compliant(metrics) and not failure and metrics["token_cap_certified"]
        )
        _expect(
            record,
            {
                **{field: metrics[field] for field in COST_FIELDS},
                **{field: verification[field] for field in
                   ("patch_valid", "f2p_passed", "p2p_passed")},
                "resolved": resolved,
                "outcome": "error" if failure else "resolved" if resolved else "unresolved",
                "run": metrics["run"],
                "provider": metrics["provider"],
                "token_cap_certified": metrics["token_cap_certified"],
                "usage_accounting_complete": metrics["provider"]["usage_accounting_complete"],
                "budget": {field: metrics[field] for field in BUDGET_LIMITS},
                "visible_tests_passed": metrics["visible_tests_passed"],
                "termination_reason": metrics["termination_reason"],
                "seed": metrics["run"]["seed"],
                "backend": "endpoint",
                "runner_returncode": manifest["runner_returncode"],
                "error": error,
            },
            f"{path}: record {record['task_id']}",
        )
        output_dir = record.get("output_dir")
        # This is a historical host path, not a location to read or execute.
        if not isinstance(output_dir, str) or PurePosixPath(output_dir).name != task["name"]:
            raise SubmissionError(f"{path}: output_dir must identify {task['name']}")
    resolved = sum(record["resolved"] for record in records)
    low, high = wilson_interval(resolved, 12)
    _expect(batch, {
        "tasks": 12,
        "resolved": resolved,
        "runner_failures": sum(record["outcome"] == "error" for record in records),
        "valid_unresolved": sum(record["outcome"] == "unresolved" for record in records),
        "usage_uncertified": sum(not record["usage_accounting_complete"] for record in records),
        "resolve_rate": resolved / 12,
        "resolve_rate_wilson_95": {"low": low, "high": high},
        "task_order": ids,
        "cost": {
            field: {
                "mean": statistics.fmean(record[field] for record in records),
                "median": float(statistics.median(record[field] for record in records)),
                "max": float(max(record[field] for record in records)),
            }
            for field in COST_FIELDS
        },
    }, str(path))
    _check_csv(path.with_name("tasks.csv"), list(TASK_COLUMNS), records)
    return batch, identities[0]


def _check_episode(
    episode: Path, task: dict, configs: dict[str, Path], root: Path, source_cache: dict,
) -> dict:
    for name in EPISODE_FILES:
        _require_file(episode / name, f"episode {episode.name} {name}")
    metrics = _load_json_object(episode / "metrics.json", str(episode / "metrics.json"))
    state = _load_json_object(episode / "final_state.json", str(episode / "final_state.json"))
    missing = sorted({
        *COST_FIELDS, *BUDGET_LIMITS, "run", "tool_calls", "patch_nonempty", "resolved",
        "visible_tests_passed", "termination_reason",
    } - metrics.keys())
    if missing:
        raise SubmissionError(f"{episode}: metrics missing required fields: {missing}")
    run = metrics.get("run")
    if not isinstance(run, dict) or type(run.get("seed")) is not int:
        raise SubmissionError(f"{episode}: metrics run identity requires an integer seed")
    digest = run.get("config_sha256")
    if not isinstance(digest, str) or digest not in configs:
        raise SubmissionError(f"{episode}: config_sha256 must match a packaged config JSON")
    expected = {
        "identity_version": 2,
        "seed": run["seed"],
        "backend": "endpoint",
        "config_sha256": digest,
        "task_input_sha256": task["sha256"],
    }
    _check_episode_schema(episode, metrics, state, expected)
    config = _load_json_object(configs[digest], str(configs[digest]))
    _expect(run.get("config"), config, f"{episode}: config snapshot")
    _check_run_source(root, episode, run, source_cache)
    _check_provider_identity(run, episode)
    _expect(run.get("sampling"), {
        "temperature": config.get("temperature", 0.2),
        "top_p": config.get("top_p", 1.0),
        "max_output_tokens": config.get("max_output_tokens", 4096),
    }, f"{episode}: sampling")
    for field, (config_field, hard_cap) in BUDGET_LIMITS.items():
        value = config.get(config_field, hard_cap)
        _nonnegative(value, f"{episode}: config {config_field}")
        if not 0 < value <= hard_cap:
            raise SubmissionError(f"{episode}: config {config_field} exceeds episode budget")
        # run_agent casts numeric call/token config limits to integers.
        _expect(metrics[field], float(value) if field == "max_active_seconds" else int(value),
                f"{episode}: {field} vs config")
    for field, default, lower, upper in (
        ("temperature", 0.2, 0, 2), ("top_p", 1.0, 0, 1),
        ("max_output_tokens", 4096, 0, 4096),
    ):
        value = config.get(field, default)
        _nonnegative(value, f"{episode}: config {field}", integer=field == "max_output_tokens")
        if value > upper or value < lower or (field != "temperature" and value == 0):
            raise SubmissionError(f"{episode}: config {field} is out of range")
    for field in ("bootstrap_context", "auto_test_after_write"):
        if type(config.get(field, False)) is not bool:
            raise SubmissionError(f"{episode}: config {field} must be boolean")
    if metrics["test_seconds"] > metrics["active_seconds"]:
        raise SubmissionError(f"{episode}: test_seconds exceeds active_seconds")
    _expect(state["issue"], task["issue"], f"{episode}: final_state issue/task identity")
    _check_trajectory(episode / "trajectory.jsonl", metrics, state)
    _check_verification(episode, metrics["patch_nonempty"], task)
    _check_provider(episode, metrics)
    try:
        validate_episode_manifest(episode, run)
    except ValueError as error:
        raise SubmissionError(f"{episode}: {error}") from error
    return metrics


def _budget_diagnostics(metrics: dict) -> list[str]:
    """Recognize only the old batch runner's budget artifact diagnostics."""
    if metrics["termination_reason"] != "budget_exhausted":
        return []
    return [
        f"metrics.json {field} exceeds {limit}"
        for field, limit in (
            ("model_calls", "max_model_calls"), ("total_tokens", "max_tokens"),
            ("active_seconds", "max_active_seconds"),
        )
        if metrics[field] > metrics[limit]
    ]


def _check_provider_identity(run: dict, episode: Path) -> None:
    if not isinstance(run.get("model_id"), str) or not run["model_id"].strip():
        raise SubmissionError(f"{episode}: model_id must identify the requested model")
    try:
        url = urlsplit(run["base_url"])
        valid = (
            url.hostname and not url.username and not url.password and not url.query
            and not url.fragment and (url.scheme == "https" or
                (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))
        )
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise SubmissionError(f"{episode}: invalid provider base_url")
    if run.get("action_mode") not in {"native", "json", "auto", "native_tools", "json_text"}:
        raise SubmissionError(f"{episode}: invalid action_mode")
    if run.get("reasoning_effort") is not None and not isinstance(run["reasoning_effort"], str):
        raise SubmissionError(f"{episode}: invalid reasoning_effort")
    runtime = run.get("runtime")
    if not isinstance(runtime, dict) or any(
        not isinstance(runtime.get(field), str) or not runtime[field]
        for field in ("python", "implementation", "platform", "machine")
    ) or not isinstance(runtime.get("dependencies"), dict):
        raise SubmissionError(f"{episode}: missing runtime identity")
    if not isinstance(runtime.get("executable_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", runtime["executable_sha256"]
    ):
        raise SubmissionError(f"{episode}: missing Python executable identity")
    try:
        runtime_comparison_identity(runtime)
    except ValueError as error:
        raise SubmissionError(f"{episode}: {error}") from error


def _check_run_source(root: Path, episode: Path, run: dict, source_cache: dict) -> None:
    files = run.get("source_files")
    if not isinstance(files, dict) or not files or run.get("source_sha256") != object_sha256(files):
        raise SubmissionError(f"{episode}: source_sha256/source_files mismatch")
    candidates = [root, episode.parent / "sources" / run["source_sha256"]]
    actual = None
    for candidate in candidates:
        if candidate not in source_cache:
            source_cache[candidate] = source_files(candidate) if candidate.is_dir() else None
            if source_cache[candidate] is not None:
                _check_integrity(candidate, source_snapshot=candidate != root)
        if source_cache[candidate] == files:
            actual = candidate
            break
    if actual is None:
        raise SubmissionError(f"{episode}: exact run source snapshot is missing or modified")


def _check_provider(episode: Path, metrics: dict) -> None:
    """Reconcile request attempts, retry charges and uncertain usage independently."""
    provider = _load_json_object(episode / "provider_metrics.json", str(episode))
    _expect(metrics.get("provider"), provider, f"{episode}: provider")
    integer_fields = (
        "logical_calls", "http_attempts", "retries", "http_429", "http_5xx",
        "unknown_usage_attempts", "reserved_unknown_tokens", "reported_total_tokens",
        "reported_reasoning_tokens", "reasoning_usage_reported_attempts",
    )
    for field in integer_fields:
        _nonnegative(provider.get(field), f"{episode}: provider.{field}", integer=True)
    for field in ("request_seconds", "pacing_wait_seconds", "provider_wait_seconds",
                  "provider_requested_wait_seconds"):
        _nonnegative(provider.get(field), f"{episode}: provider.{field}")
    spent = sum(provider[field] for field in
                ("request_seconds", "pacing_wait_seconds", "provider_wait_seconds"))
    if spent > metrics["active_seconds"] + 1e-6:
        raise SubmissionError(f"{episode}: active time omits API latency or retry wait")
    try:
        events = [_parse_json(line) for line in (episode / "api_attempts.jsonl").read_text().splitlines()
                  if line.strip()]
    except (OSError, UnicodeError, ValueError) as error:
        raise SubmissionError(f"{episode}: invalid api_attempts.jsonl") from error
    starts, completed = {}, {}
    for event in events:
        if not isinstance(event, dict):
            raise SubmissionError(f"{episode}: API attempt must be an object")
        attempt = event.get("attempt")
        _nonnegative(attempt, f"{episode}: API attempt", integer=True)
        if event.get("phase") not in {"request_started", "response_received"}:
            raise SubmissionError(f"{episode}: invalid API phase")
        target = starts if event["phase"] == "request_started" else completed
        if attempt in target or not 1 <= attempt <= provider["http_attempts"]:
            raise SubmissionError(f"{episode}: duplicate/invalid API attempt")
        target[attempt] = event
        _expect(event.get("requested_model"), metrics["run"]["model_id"], f"{episode}: requested_model")
        _expect(event.get("base_url"), metrics["run"]["base_url"], f"{episode}: API base_url")
        _expect(event.get("action_mode"), metrics["run"]["action_mode"], f"{episode}: API action_mode")
        _expect(event.get("generation"), {
            "temperature": metrics["run"]["sampling"]["temperature"],
            "top_p": metrics["run"]["sampling"]["top_p"],
            "max_tokens": metrics["run"]["sampling"]["max_output_tokens"],
            "reasoning_effort": metrics["run"]["reasoning_effort"],
        }, f"{episode}: API generation settings")
        request = event.get("request")
        _expect(request, {
            "model": metrics["run"]["model_id"],
            "temperature": metrics["run"]["sampling"]["temperature"],
            "top_p": metrics["run"]["sampling"]["top_p"],
            "max_tokens": metrics["run"]["sampling"]["max_output_tokens"],
        }, f"{episode}: API request")
        if not isinstance(request.get("messages"), list) or not request["messages"]:
            raise SubmissionError(f"{episode}: API request messages missing")
        _nonnegative(event.get("logical_call"), f"{episode}: logical_call", integer=True)
        if not 1 <= event["logical_call"] <= provider["logical_calls"]:
            raise SubmissionError(f"{episode}: invalid logical_call")
    expected = set(range(1, provider["http_attempts"] + 1))
    if set(starts) != expected or set(completed) != expected:
        raise SubmissionError(f"{episode}: missing API request/response accounting")
    model_events = [
        event["payload"] for event in (
            _parse_json(line) for line in (episode / "trajectory.jsonl").read_text().splitlines()
            if line.strip()
        ) if event.get("kind") == "model"
    ]
    returned_events = [
        event for event in completed.values()
        if type(event.get("http_status")) is int and 200 <= event["http_status"] < 300
        and not event.get("error") and event.get("usage_status") == "reported"
        and event.get("response_delivered") is True
    ]
    if metrics["run"]["backend"] != "replay":
        if len(model_events) != len(returned_events):
            raise SubmissionError(f"{episode}: missing model response in trajectory/API evidence")
        for model, event in zip(model_events, returned_events):
            _expect(model, {
                "content": event.get("normalized_action_text", event.get("final_answer_text")),
                "input_tokens": event.get("input_tokens"),
                "output_tokens": event.get("output_tokens"),
            }, f"{episode}: model/API response")
    totals = {"input_tokens": 0, "output_tokens": 0}
    unknown = reserved = http429 = http5xx = reported = reasoning = reasoning_attempts = 0
    returned, seconds = [], 0.0
    for attempt in sorted(completed):
        event = completed[attempt]
        if event["logical_call"] != starts[attempt]["logical_call"]:
            raise SubmissionError(f"{episode}: API request/response logical_call mismatch")
        status = event.get("http_status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise SubmissionError(f"{episode}: invalid http_status")
        if status is not None and 200 <= status < 300:
            if type(event.get("response_delivered")) is not bool:
                raise SubmissionError(f"{episode}: missing response_delivered evidence")
            if not event["response_delivered"] and not event.get("error"):
                exceeded = any(metrics[field] > metrics[limit] for field, limit in (
                    ("model_calls", "max_model_calls"), ("total_tokens", "max_tokens"),
                    ("active_seconds", "max_active_seconds"),
                ))
                if not exceeded or not isinstance(event.get("budget_error"), str) or not event["budget_error"]:
                    raise SubmissionError(f"{episode}: undelivered response requires budget/error evidence")
        _nonnegative(event.get("request_seconds"), f"{episode}: request_seconds")
        seconds += event["request_seconds"]
        http429 += status == 429
        http5xx += status is not None and 500 <= status <= 599
        if event.get("returned_model") is not None:
            if not isinstance(event["returned_model"], str):
                raise SubmissionError(f"{episode}: invalid returned_model")
            if event["returned_model"] not in returned:
                returned.append(event["returned_model"])
        if event.get("usage_status") == "reported":
            # Pure accounting helper: no client construction, credentials or IO.
            from harness.model_client import account_usage
            try:
                accounting = account_usage(event.get("usage_raw"))
            except (ValueError, RuntimeError) as error:
                raise SubmissionError(f"{episode}: invalid reported usage") from error
            _expect(event, accounting, f"{episode}: API usage accounting")
            charges = (accounting["input_tokens"], accounting["output_tokens"])
            reported += accounting["reported_total_tokens"]
            if accounting["reported_reasoning_tokens"] is not None:
                reasoning += accounting["reported_reasoning_tokens"]
                reasoning_attempts += 1
        elif event.get("usage_status") == "unknown_reserved":
            unknown += 1
            reservation = event.get("unknown_usage_reservation_tokens")
            _nonnegative(reservation, f"{episode}: unknown usage reservation", integer=True)
            if reservation <= 0:
                raise SubmissionError(f"{episode}: unknown usage requires a positive reservation")
            reserved += reservation
            charges = (event.get("charged_input_tokens"), event.get("charged_output_tokens"))
            if charges != (0, reservation):
                raise SubmissionError(f"{episode}: unknown usage charge mismatch")
            raw = event.get("usage_raw")
            if isinstance(raw, dict):
                known = {key: value for key, value in raw.items() if type(value) is int and value >= 0}
                reported += known.get("total_tokens", 0)
                nested = raw.get("completion_tokens_details")
                nested_reasoning = nested.get("reasoning_tokens") if isinstance(nested, dict) else None
                event_reasoning = max(
                    known.get("reasoning_tokens", 0),
                    nested_reasoning if type(nested_reasoning) is int and nested_reasoning >= 0 else 0,
                )
                lower_bound = max(
                    known.get("total_tokens", 0),
                    known.get("prompt_tokens", 0) + known.get("completion_tokens", 0)
                    + event_reasoning,
                )
                if reservation < lower_bound:
                    raise SubmissionError(f"{episode}: unknown usage reservation ignores reported lower bound")
                if "reasoning_tokens" in known or type(nested_reasoning) is int:
                    reasoning += event_reasoning
                    reasoning_attempts += 1
        else:
            raise SubmissionError(f"{episode}: invalid usage_status")
        for field, charge in zip(totals, charges):
            _nonnegative(charge, f"{episode}: charged {field}", integer=True)
            totals[field] += charge
    replay = metrics["run"]["backend"] == "replay"
    if replay and events:
        raise SubmissionError(f"{episode}: replay must have no HTTP attempts")
    if not replay:
        for field, total in totals.items():
            _expect(metrics[field], total, f"{episode}: API {field}")
    _expect(metrics["model_calls"], provider["logical_calls"] + provider["retries"],
            f"{episode}: retries count as model calls")
    if provider["http_attempts"] > metrics["model_calls"]:
        raise SubmissionError(f"{episode}: uncharged API attempt")
    if provider["retries"] != provider["http_attempts"] - len({e["logical_call"] for e in completed.values()}):
        raise SubmissionError(f"{episode}: retry count mismatch")
    _expect(provider, {
        "unknown_usage_attempts": unknown, "reserved_unknown_tokens": reserved,
        "http_429": http429, "http_5xx": http5xx, "request_seconds": seconds,
        "usage_accounting_complete": unknown == 0,
        "returned_models": ["deterministic-replay"] if replay else returned,
        **({} if replay else {
            "reported_total_tokens": reported, "reported_reasoning_tokens": reasoning,
            "reasoning_usage_reported_attempts": reasoning_attempts,
        }),
    }, f"{episode}: provider")
    _expect(metrics.get("token_cap_certified"), unknown == 0 and metrics["total_tokens"] <= metrics["max_tokens"],
            f"{episode}: token_cap_certified")
    _expect(metrics.get("api_latency_and_retry_wait_exempted"), False, f"{episode}: active time exemption")


def _check_episode_schema(episode: Path, metrics: dict, state: dict, expected_run: dict) -> None:
    # Submission evidence is not a runtime enforcement gate. An in-flight request
    # or tool may finish over the soft token/time limit before Budget.check raises.
    # Validate the original counters, without clamping or rewriting the artifacts.
    for field in (*COST_FIELDS, *BUDGET_LIMITS):
        _nonnegative(metrics[field], f"{episode}: {field}", integer=field in INTEGER_METRICS)
    termination = metrics["termination_reason"]
    if not isinstance(termination, str) or termination not in {"finished", "budget_exhausted", "error", "interrupted"}:
        raise SubmissionError(f"{episode}: invalid termination_reason")
    for field, limit in (
        ("model_calls", "max_model_calls"), ("total_tokens", "max_tokens"),
        ("active_seconds", "max_active_seconds"),
    ):
        if metrics[field] > metrics[limit] and termination not in {"budget_exhausted", "error", "interrupted"}:
            raise SubmissionError(f"{episode}: {field} exceeds {limit} without valid termination")
    _expect(metrics["total_tokens"], metrics["input_tokens"] + metrics["output_tokens"],
            f"{episode}: total_tokens")
    counts = metrics["tool_calls"]
    if not isinstance(counts, dict):
        raise SubmissionError(f"{episode}: tool_calls must be an object")
    for name, count in counts.items():
        _nonnegative(count, f"{episode}: tool_calls.{name}", integer=True)
    visible = metrics["visible_tests_passed"]
    if visible is not None and type(visible) is not bool:
        raise SubmissionError(f"{episode}: visible_tests_passed must be boolean or null")
    try:
        nonempty = bool((episode / "patch.diff").read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError) as error:
        raise SubmissionError(f"{episode}: patch.diff must be UTF-8 text") from error
    _expect(metrics["patch_nonempty"], nonempty, f"{episode}: patch_nonempty")
    _expect(metrics["resolved"], nonempty and visible is True, f"{episode}: resolved")
    _expect(metrics["run"], expected_run, f"{episode}: metrics run identity")
    for field in FINAL_STATE_FIELDS:
        if field not in state:
            raise SubmissionError(f"{episode}: final_state missing {field}")
    for field in ("issue", "status", "summary", "last_observation"):
        if not isinstance(state[field], str):
            raise SubmissionError(f"{episode}: final_state {field} must be a string")
    _nonnegative(state["step"], f"{episode}: final_state step", integer=True)
    _expect(state["status"], termination, f"{episode}: final_state status")
    _expect(state["visible_tests_passed"], visible, f"{episode}: final_state visible_tests_passed")


def _check_trajectory(path: Path, metrics: dict, state: dict) -> None:
    try:
        events = [
            _parse_json(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, ValueError) as error:
        raise SubmissionError(f"{path}: invalid trajectory JSONL") from error
    requests = responses = input_tokens = output_tokens = 0
    tools: dict[str, int] = {}
    states = []
    budgets = []
    finishes = []
    errors = []
    last_request = last_model = -1
    last_request_step = None
    for index, event in enumerate(events, 1):
        label = f"{path}: event {index}"
        if (not isinstance(event, dict) or not isinstance(event.get("kind"), str)
                or not event["kind"] or not isinstance(event.get("payload"), dict)):
            raise SubmissionError(f"{label}: expected kind and payload object")
        kind, payload = event["kind"], event["payload"]
        if "step" in payload:
            _nonnegative(payload["step"], f"{label}: step", integer=True)
            if payload["step"] > state["step"]:
                raise SubmissionError(f"{label}: step exceeds final_state")
        if kind in {"model_request", "model", "tool", "state", "finish", "error"}:
            _nonnegative(payload.get("step"), f"{label}: step", integer=True)
        if kind == "model_request":
            _nonnegative(payload.get("messages"), f"{label}: messages", integer=True)
            requests += 1
            last_request, last_request_step = index, payload["step"]
        elif kind == "model":
            if not isinstance(payload.get("content"), str):
                raise SubmissionError(f"{label}: model content must be a string")
            for field in ("input_tokens", "output_tokens"):
                _nonnegative(payload.get(field), f"{label}: {field}", integer=True)
            responses += 1
            last_model = index
            input_tokens += payload["input_tokens"]
            output_tokens += payload["output_tokens"]
            if responses > requests:
                raise SubmissionError(f"{label}: model response has no request")
        elif kind == "tool":
            name = payload.get("name")
            if (not isinstance(name, str) or not name
                    or not isinstance(payload.get("arguments"), dict)
                    or not isinstance(payload.get("observation"), str)
                    or ("automatic" in payload and type(payload["automatic"]) is not bool)):
                raise SubmissionError(f"{label}: invalid tool record")
            tools[name] = tools.get(name, 0) + 1
        elif kind == "state":
            if not isinstance(payload.get("status"), str):
                raise SubmissionError(f"{label}: invalid state status")
            states.append(payload)
        elif kind in {"finish", "error"}:
            field = "summary" if kind == "finish" else "message"
            if not isinstance(payload.get(field), str):
                raise SubmissionError(f"{label}: invalid {field}")
            if kind == "finish":
                finishes.append(payload)
            else:
                errors.append(payload)
        elif kind == "budget":
            for field in (*COST_FIELDS, *BUDGET_LIMITS):
                value = payload.get(field)
                _nonnegative(value, f"{label}: {field}", integer=field in INTEGER_METRICS)
                if field in BUDGET_LIMITS:
                    _expect(value, metrics[field], f"{label}: {field}")
                elif value > metrics[field] or (budgets and value < budgets[-1][field]):
                    raise SubmissionError(f"{label}: inconsistent budget {field}")
            _expect(payload["total_tokens"], payload["input_tokens"] + payload["output_tokens"],
                    f"{label}: total_tokens")
            counts = payload.get("tool_calls")
            if not isinstance(counts, dict):
                raise SubmissionError(f"{label}: invalid budget tool_calls")
            for name, count in counts.items():
                _nonnegative(count, f"{label}: tool_calls.{name}", integer=True)
                if count > metrics["tool_calls"].get(name, 0):
                    raise SubmissionError(f"{label}: inconsistent budget tool_calls")
            budgets.append(payload)
        # Students may add event kinds and tool names; the shared evidence envelope
        # and accounting records stay mandatory.
    interrupted = bool(
        _budget_diagnostics(metrics)
        and errors
        and errors[-1]["step"] == state["step"]
        and errors[-1]["message"]
    )
    pending_model = (
        interrupted and last_request > last_model and last_request_step == state["step"]
        and bool(budgets)
        and any(budgets[-1][field] < metrics[field] for field in ("input_tokens", "output_tokens"))
    )
    # Only older traces ending with a pre-charge snapshot can lack a returned
    # response. The current runner retains the response and terminal budget;
    # that charged snapshot must not excuse a deleted response record.
    failed = metrics["termination_reason"] in {"error", "interrupted", "budget_exhausted"}
    if metrics["termination_reason"] in {"error", "interrupted"} and not errors:
        raise SubmissionError(f"{path}: missing error/interruption diagnosis")
    if (not responses and not pending_model and not failed) or not states or (requests and not budgets):
        raise SubmissionError(f"{path}: missing model, budget, or state records")
    provider = metrics.get("provider", {})
    _expect(requests, provider.get("logical_calls", metrics["model_calls"]), f"{path}: model request count")
    for field, logged in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if provider.get("http_attempts"):
            if logged > metrics[field]:
                raise SubmissionError(f"{path}: model {field} exceeds charged API usage")
        elif not pending_model or logged > metrics[field]:
            _expect(logged, metrics[field], f"{path}: {field}")
        elif any(
            budget[field] > logged for budget in budgets
            if budget["model_calls"] < metrics["model_calls"]
        ):
            raise SubmissionError(f"{path}: missing model accounting before interrupted request")
    expected_tools = {name: count for name, count in metrics["tool_calls"].items() if count}
    missing_tools = sum(expected_tools.values()) - sum(tools.values())
    pending_tool = (
        interrupted and metrics["active_seconds"] > metrics["max_active_seconds"]
        and not pending_model and missing_tools == 1
        and all(count <= expected_tools.get(name, 0) for name, count in tools.items())
    )
    if tools != expected_tools and not pending_tool:
        raise SubmissionError(f"{path}: tool counts do not match metrics")
    _expect(states[-1], {"status": state["status"], "step": state["step"]},
            f"{path}: final state")
    # The last pre-operation snapshot can legitimately precede the charge that
    # raised BudgetExceeded. All snapshots must still be monotone and bounded by
    # the final counters; permit a gap only for the corresponding interrupted op.
    for field in COST_FIELDS:
        if not budgets:
            if metrics[field] and field != "active_seconds":
                raise SubmissionError(f"{path}: missing budget records")
            continue
        if field == "active_seconds":
            continue
        if pending_model and field in {"input_tokens", "output_tokens", "total_tokens"}:
            continue
        if pending_model and field == "model_calls":
            if metrics[field] - budgets[-1][field] in {0, 1}:
                continue
        if pending_tool and field in {"test_invocations", "test_seconds"}:
            if field == "test_seconds" or metrics[field] - budgets[-1][field] in {0, 1}:
                continue
        _expect(budgets[-1][field], metrics[field], f"{path}: final budget.{field}")
    if not pending_tool and budgets:
        _expect(budgets[-1]["tool_calls"], metrics["tool_calls"], f"{path}: final budget.tool_calls")
    if finishes and state["status"] == "finished":
        _expect(finishes[-1]["summary"], state["summary"], f"{path}: final summary")
    # Custom tools may invoke tests under other names. Only the standard action
    # has a known test-accounting meaning.
    if tools.get("run_tests", 0) > metrics["test_invocations"]:
        raise SubmissionError(f"{path}: test_invocations is inconsistent with run_tests")


def _check_verification(episode: Path, patch_nonempty: bool, task: dict) -> None:
    path = episode / "verification.json"
    verification = _load_json_object(path, str(path))
    _expect(verification.get("patch_sha256"), _sha256(episode / "patch.diff"), f"{path}: patch_sha256")
    _expect(verification.get("task_input_sha256"), task["sha256"], f"{path}: task_input_sha256")
    flags = ("resolved", "patch_valid", "f2p_passed", "p2p_passed", "protected_paths_clean")
    for field in flags:
        if type(verification.get(field)) is not bool:
            raise SubmissionError(f"{path}: {field} must be boolean")
    details = verification.get("details")
    if not isinstance(details, dict):
        raise SubmissionError(f"{path}: details must be an object")
    if "patch_error" in details and not isinstance(details["patch_error"], str):
        raise SubmissionError(f"{path}: patch_error must be a string")
    for field in ("header_paths", "changed_paths", "protected_hits", "symlink_hits"):
        if field == "header_paths" or verification["patch_valid"] or field in details:
            values = details.get(field)
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise SubmissionError(f"{path}: invalid details.{field}")
    if not patch_nonempty and any(verification[field] for field in flags):
        raise SubmissionError(f"{path}: empty failed patch cannot pass verification")
    valid = verification["patch_valid"]
    clean = verification["protected_paths_clean"]
    # Header paths are diagnostics, not a proof that git applied the patch.
    # Early failures (e.g. a size limit) can precede header extraction.
    if valid or "patch_error" not in details:
        headers = sorted(changed_paths((episode / "patch.diff").read_text(encoding="utf-8")))
        _expect(details["header_paths"], headers, f"{path}: patch header_paths")
    if clean and (details.get("protected_hits") or details.get("symlink_hits")):
        raise SubmissionError(f"{path}: protected_paths_clean contradicts protected paths")
    if valid and (not clean or details["protected_hits"] or details["symlink_hits"]):
        raise SubmissionError(f"{path}: patch_valid contradicts protected paths")
    if not valid and (verification["f2p_passed"] or verification["p2p_passed"]):
        raise SubmissionError(f"{path}: invalid patch cannot pass acceptance tests")
    for field in ("f2p", "p2p"):
        if valid or field in details:
            result = details.get(field)
            if (not isinstance(result, dict) or type(result.get("returncode")) is not int
                    or not isinstance(result.get("stdout"), str)
                    or not isinstance(result.get("stderr"), str)):
                raise SubmissionError(f"{path}: invalid details.{field} test result")
            # A zero exit code need not mean tests completed (e.g. early exit).
            # Preserve the verifier's stricter completion verdict when present.
            if result["returncode"] != 0 and verification[f"{field}_passed"]:
                raise SubmissionError(f"{path}: {field}_passed contradicts returncode")
            _check_test_result(result, task[field], f"{path}: details.{field}")
            _expect(verification[f"{field}_passed"], result["returncode"] == 0,
                    f"{path}: {field}_passed")
    _expect(verification["resolved"],
            valid and clean and verification["f2p_passed"] and verification["p2p_passed"],
            f"{path}: resolved")


def _check_test_result(result: dict, requested: list[str], label: str) -> None:
    """Reconcile the current verifier's completion report, including failed runs."""
    report = result.get("test_report")
    report_error = result.get("report_error")
    if report_error is not None and (not isinstance(report_error, str) or not report_error):
        raise SubmissionError(f"{label}: invalid report_error")
    process_code = result.get("process_returncode")
    if "process_returncode" in result and type(process_code) is not int:
        raise SubmissionError(f"{label}: process_returncode must be an integer")
    if report is None:
        # Timeouts, collection/protection failures and early exits may produce
        # diagnostics without a completed test report, but never a passing result.
        if result["returncode"] == 0 or not report_error:
            raise SubmissionError(f"{label}: missing test_report/completion evidence")
        return
    if not isinstance(report, dict) or type(process_code) is not int:
        raise SubmissionError(f"{label}: invalid test_report schema")
    _expect(report.get("version"), 1, f"{label}: test_report.version")
    if type(report.get("completed")) is not bool:
        raise SubmissionError(f"{label}: test_report.completed must be boolean")
    for field in ("session_exitstatus", "exitstatus"):
        if type(report.get(field)) is not int:
            raise SubmissionError(f"{label}: invalid test_report.{field}")
    for field in ("collected", "deselected", "collection_issues", "tests"):
        if not isinstance(report.get(field), list):
            raise SubmissionError(f"{label}: invalid test_report.{field}")
    if any(not isinstance(node, str) for node in report["collected"]):
        raise SubmissionError(f"{label}: invalid collected test IDs")
    for item in report["tests"]:
        if (not isinstance(item, dict) or not isinstance(item.get("nodeid"), str)
                or item.get("when") not in {"setup", "call", "teardown"}
                or item.get("outcome") not in {"passed", "failed", "skipped"}
                or type(item.get("wasxfail")) is not bool):
            raise SubmissionError(f"{label}: invalid per-test report")
    failure = _test_report_error(report, requested, process_code)
    expected_code = (process_code or 1) if failure else process_code
    _expect(result["returncode"], expected_code, f"{label}: test_report returncode")
    if bool(report_error) != bool(failure):
        raise SubmissionError(f"{label}: report_error contradicts test completion")


def _check_paired(path: Path, batches: dict, baselines: list) -> None:
    paired = _load_json_object(path, str(path))
    conditions = paired.get("conditions")
    comparisons = paired.get("comparisons_to_first_condition")
    if (not isinstance(conditions, dict) or len(conditions) < 3
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", label)
                   for label in conditions)
            or not isinstance(comparisons, dict)):
        raise SubmissionError(f"{path}: require Full (first/reference condition) plus at least two paired ablations")
    # report.py serializes JSON with sort_keys=True, so JSON key order does NOT
    # identify the first condition. The comparison references do.
    first = set(conditions) - set(comparisons)
    if len(first) != 1 or set(comparisons) != set(conditions) - first:
        raise SubmissionError(f"{path}: invalid comparison condition labels")
    baseline = first.pop()
    labels = [baseline, *(label for label in conditions if label != baseline)]
    validated = {}
    identities = {}
    for label in labels:
        directory = path.parent / "conditions" / label
        if directory not in batches:
            raise SubmissionError(f"{path}: missing condition summary/evidence: {label}")
        batch, identity = batches[directory]
        _expect(batch["split"], f"public-dev:{label}", f"{path}: condition split")
        validated[label] = batch
        identities[label] = identity
        _expect(paired.get("task_order"), batch["task_order"], f"{path}: task_order")
        _expect(paired.get("seed"), identity["seed"], f"{path}: seed")
        _expect(conditions[label], {
            **{field: batch[field] for field in
               ("tasks", "resolved", "resolve_rate", "resolve_rate_wilson_95")},
            **{f"mean_{field}": batch["cost"][field]["mean"]
               for field in ("model_calls", "total_tokens", "active_seconds")},
        }, f"{path}: conditions.{label}")
    base = validated[baseline]
    budget_variable = paired.get("budget_variable")
    if type(budget_variable) is not bool:
        raise SubmissionError(f"{path}: budget_variable declaration missing")
    fixed = comparison_identity(identities[baseline])
    _expect(paired.get("comparison_identity"), fixed, f"{path}: comparison_identity")
    for label, batch in validated.items():
        _expect(comparison_identity(identities[label]), fixed, f"{path}: model/provider/settings/runtime")
        if not budget_variable:
            for record in batch["records"]:
                _expect(record["budget"], base["records"][0]["budget"], f"{path}: equal family budgets")
    if not any(
        identity["seed"] == paired["seed"] and batch["task_order"] == paired["task_order"]
        and comparison_identity(identity) == fixed
        for batch, identity in baselines
    ):
        raise SubmissionError(
            f"{path}: paired seed/task_order/model/provider/settings/runtime compatibility "
            "differs from independent Naive baseline"
        )
    for label in labels[1:]:
        candidate = validated[label]
        wins = sum(not a["resolved"] and b["resolved"]
                   for a, b in zip(base["records"], candidate["records"]))
        losses = sum(a["resolved"] and not b["resolved"]
                     for a, b in zip(base["records"], candidate["records"]))
        _expect(comparisons[label], {
            "baseline": baseline,
            "wins": wins,
            "losses": losses,
            "ties": 12 - wins - losses,
            "resolve_rate_delta": candidate["resolve_rate"] - base["resolve_rate"],
        }, f"{path}: comparisons.{label}")
    columns = ["task_id", *(
        f"{label}.{field}" for label in labels
        for field in ("resolved", "model_calls", "total_tokens", "active_seconds")
    )]
    rows = [
        {"task_id": task_id, **{
            f"{label}.{field}": validated[label]["records"][index][field]
            for label in labels
            for field in ("resolved", "model_calls", "total_tokens", "active_seconds")
        }}
        for index, task_id in enumerate(base["task_order"])
    ]
    _check_csv(path.with_name("paired_tasks.csv"), columns, rows, paired_baseline=baseline)


def _expect(actual: object, expected: object, label: str) -> None:
    """Compare required schema fields, allowing student-added diagnostic metadata."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise SubmissionError(f"{label}: expected an object")
        for key, value in expected.items():
            if key not in actual:
                raise SubmissionError(f"{label}: missing {key}")
            _expect(actual[key], value, f"{label}.{key}")
        return
    if isinstance(expected, float):
        if (type(actual) in {int, float} and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)):
            return
    elif type(actual) is type(expected) and actual == expected:
        return
    raise SubmissionError(f"{label}: inconsistent value")


def _nonnegative(value: object, label: str, *, integer: bool = False) -> None:
    if (type(value) not in ({int} if integer else {int, float})
            or not math.isfinite(value) or value < 0):
        raise SubmissionError(f"{label}: must be a finite nonnegative {'integer' if integer else 'number'}")


def _parse_json(text: str) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def finite_number(text: str, conversion):
        value = conversion(text)
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("JSON number is outside the finite numeric range")
        return value

    return json.loads(
        text, parse_constant=reject_constant, object_pairs_hook=unique_object,
        parse_int=lambda value: finite_number(value, int),
        parse_float=lambda value: finite_number(value, float),
    )


def _load_json_object(path: Path, label: str) -> dict:
    try:
        payload = _parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SubmissionError(f"invalid JSON evidence: {label}") from error
    if not isinstance(payload, dict):
        raise SubmissionError(f"JSON evidence must contain an object: {label}")
    return payload


def _check_csv(
    path: Path, columns: list[str], expected: list[dict], *, paired_baseline: str | None = None,
) -> None:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            fields = reader.fieldnames
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        raise SubmissionError(f"{path}: invalid task CSV") from error
    if fields is None or len(fields) != len(columns) or set(fields) != set(columns):
        raise SubmissionError(f"{path}: task CSV columns do not match conditions/schema")
    if paired_baseline and fields[1] != f"{paired_baseline}.resolved":
        raise SubmissionError(f"{path}: first CSV condition is not the paired baseline")
    if len(rows) != len(expected):
        raise SubmissionError(f"{path}: task CSV row count is inconsistent")
    for index, (row, record) in enumerate(zip(rows, expected), 1):
        if set(row) != set(columns) or any(value is None for value in row.values()):
            raise SubmissionError(f"{path}: malformed task CSV row {index}")
        for field in columns:
            value = record[field]
            cell = row[field]
            if isinstance(value, float):
                try:
                    actual = float(cell)
                except ValueError as error:
                    raise SubmissionError(f"{path}: invalid numeric CSV value {field}") from error
                _expect(actual, value, f"{path}: row {index} {field}")
            else:
                _expect(cell, "" if value is None else str(value), f"{path}: row {index} {field}")


def _require_label(label: str) -> None:
    if not isinstance(label, str) or not LABEL_PATTERN.fullmatch(label):
        raise SubmissionError("labels must use letters, digits, '.', '_' or '-'")


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise SubmissionError("unsafe artifact path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise SubmissionError(f"unsafe artifact path: {value}")
    return path


def _supplement_file(path: Path, relative: PurePosixPath) -> None:
    _reject_forbidden_paths([relative.as_posix()])
    snapshot = any(
        part == "sources" and index + 1 < len(relative.parts)
        and re.fullmatch(r"[0-9a-f]{64}", relative.parts[index + 1])
        for index, part in enumerate(relative.parts)
    )
    allowed = (
        path.suffix.lower() in SUPPLEMENT_SUFFIXES
        or (snapshot and (path.suffix == ".py" or path.name == "Dockerfile"))
        or path.name == ".cs8803_hw2_output"
    )
    if not allowed:
        raise SubmissionError(f"unsupported supplemental artifact (not a task repository): {relative}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SubmissionError(f"supplement file exceeds {MAX_FILE_BYTES} bytes: {relative}")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise SubmissionError(f"supplement must be UTF-8 evidence: {relative}") from error


def _copy_supplement(source: Path, destination: Path) -> dict:
    """Preserve even incomplete JSON/JSONL as unscored evidence, never follow paths in it."""
    if source.is_symlink() or not source.is_dir() or source.name in (SUPPLEMENT_OMIT_DIRS - {".pilot-output"}):
        raise SubmissionError("supplement must be an evidence directory, not a symlink or repository")
    source = source.resolve()
    excluded, files, size = [], {}, 0
    for directory, children, names in os.walk(source, followlinks=False):
        parent = Path(directory)
        for name in list(children):
            child = parent / name
            if child.is_symlink():
                raise SubmissionError("supplement symlinks are not allowed")
            if name in SUPPLEMENT_OMIT_DIRS:
                excluded.append(child.relative_to(source).as_posix() + "/")
                children.remove(name)
        for name in sorted(names):
            path = parent / name
            relative = _safe_relative(path.relative_to(source).as_posix())
            if path.is_symlink() or not path.is_file():
                raise SubmissionError(f"supplement must contain regular files, not symlinks: {relative}")
            if name == ".DS_Store" or path.suffix == ".pyc":
                excluded.append(relative.as_posix())
                continue
            _supplement_file(path, relative)
            size += path.stat().st_size
            if len(files) >= MAX_PACKAGE_FILES or size > MAX_PACKAGE_BYTES:
                raise SubmissionError("supplement exceeds file-count or total-size limit")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            files[relative.as_posix()] = _sha256(target)
    if not files:
        raise SubmissionError("supplement contains no supported evidence files")
    _scan_for_secrets(destination)
    return {"source": str(source), "files": files, "excluded": sorted(excluded),
            "use": "history only; excluded from coverage and aggregate results"}


def _check_supplements(root: Path) -> dict:
    directory = root / "supplements"
    index = root / "supplements_manifest.json"
    if not index.exists():
        if directory.exists() and any(directory.rglob("*")):
            raise SubmissionError("supplement evidence requires supplements_manifest.json")
        return {}
    payload = _load_json_object(index, str(index))
    _expect(payload.get("format_version"), 1, "supplements manifest version")
    entries = payload.get("supplements")
    if not isinstance(entries, dict) or not entries:
        raise SubmissionError("supplements manifest requires named evidence directories")
    expected, size = set(), 0
    for label, metadata in entries.items():
        _require_label(label)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("files"), dict) or not metadata["files"]:
            raise SubmissionError("supplement manifest requires file hashes")
        if not isinstance(metadata.get("source"), str) or not metadata["source"]:
            raise SubmissionError("supplement manifest requires the original source path")
        if metadata.get("use") != "history only; excluded from coverage and aggregate results":
            raise SubmissionError("supplements must be explicitly excluded from aggregate results")
        excluded = metadata.get("excluded")
        if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded):
            raise SubmissionError("supplement manifest requires an exclusion inventory")
        for item in excluded:
            relative = _safe_relative(item.rstrip("/"))
            if not (
                (item.endswith("/") and relative.name in SUPPLEMENT_OMIT_DIRS)
                or relative.name == ".DS_Store" or relative.suffix == ".pyc"
            ):
                raise SubmissionError("supplement may only exclude repositories and generated caches")
        hashes = _file_hashes(directory / label)
        if metadata["files"] != hashes:
            raise SubmissionError(f"supplement {label} has missing or unindexed files")
        for relative in metadata["files"]:
            logical = _safe_relative(relative)
            path = directory / label / logical
            _require_file(path, f"supplement {label}/{relative}")
            _supplement_file(path, logical)
            expected.add(f"{label}/{relative}")
            size += path.stat().st_size
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if actual != expected or len(actual) > MAX_PACKAGE_FILES or size > MAX_PACKAGE_BYTES:
        raise SubmissionError("supplement inventory or size limit mismatch")
    return entries


def _check_file_limits(root: Path) -> None:
    count = size = 0
    if root.is_symlink():
        raise SubmissionError("evidence directory must not be a symlink")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SubmissionError("evidence symlinks are not allowed")
        if path.is_file():
            count += 1
            length = path.stat().st_size
            size += length
            if length > MAX_FILE_BYTES or count > MAX_PACKAGE_FILES or size > MAX_PACKAGE_BYTES:
                raise SubmissionError("evidence exceeds file/count/size limits")


def _stage_study(study: Path, stage: Path) -> dict:
    _check_file_limits(study)
    _check_supplements(study)
    _check_selection_ledger(study)
    _copy_evidence(study / "evidence", stage / "evidence")
    shutil.copyfile(study / "selection_ledger.json", stage / "selection_ledger.json")
    index = _load_json_object(study / "supplements_manifest.json", "study supplements")
    for label in index["supplements"]:
        _copy_supplement(study / "supplements" / label, stage / "supplements" / label)
    return index["supplements"]


def _study_episode(root: Path, condition: str, task: str) -> Path:
    batch = root / "evidence" / "baseline" if condition == "baseline" else (
        root / "evidence" / "paired" / "conditions" / condition)
    return batch / task


def _receipt_files(episode: Path) -> dict:
    for name in EPISODE_FILES:
        _require_file(episode / name, f"selected episode {episode.name} {name}")
    return _file_hashes(episode, exclude={".cs8803_hw2_output"})


def _ledger_choices(ledger: dict, supplements: dict) -> dict[tuple[str, str], str]:
    _expect(ledger.get("format_version"), 1, "selection ledger version")
    conditions = ledger.get("condition_order")
    if (not isinstance(conditions, list) or len(conditions) != 3
            or any(not isinstance(label, str) for label in conditions) or len(set(conditions)) != 3):
        raise SubmissionError("selection ledger requires exactly three paired conditions")
    for label in conditions:
        _require_label(label)
        if label == "baseline":
            raise SubmissionError("'baseline' is reserved for the independent Naive run")
    if type(ledger.get("budget_variable")) is not bool:
        raise SubmissionError("selection ledger needs a boolean budget_variable")
    tasks = [task.name for task in discover_tasks(ROOT / "tasks/dev")]
    _expect(ledger.get("task_order"), tasks, "selection ledger task_order")
    initial = ledger.get("initial")
    labels = ["baseline", *conditions]
    if not isinstance(initial, dict) or set(initial) != set(labels):
        raise SubmissionError("selection ledger requires all original run roots")
    choices, used, previous = {}, set(), set()

    def accept_source(source):
        _require_label(source)
        if source not in supplements or source in used:
            raise SubmissionError("selection ledger has a missing or reused source root")
        path = supplements[source].get("source")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise SubmissionError("selection ledger requires canonical absolute source paths")
        canonical = str(Path(path).resolve())
        if canonical in previous:
            raise SubmissionError("all original/replacement runs must use globally unique new output directories")
        previous.add(canonical)
        used.add(source)

    for condition in labels:
        accept_source(initial[condition])
        for task in tasks:
            choices[condition, task] = initial[condition]
    blocks = ledger.get("blocks")
    if not isinstance(blocks, list):
        raise SubmissionError("selection ledger requires a replacement block list")
    for block in blocks:
        if not isinstance(block, dict) or block.get("kind") not in {"baseline", "paired"}:
            raise SubmissionError("invalid replacement block")
        affected, sources = block.get("tasks"), block.get("sources")
        expected = {"baseline"} if block["kind"] == "baseline" else set(conditions)
        if not isinstance(sources, dict) or set(sources) != expected:
            raise SubmissionError("paired replacement must include all three conditions for the same task block")
        if (not isinstance(affected, list) or not affected
                or any(not isinstance(task, str) or task not in tasks for task in affected)
                or len(set(affected)) != len(affected)):
            raise SubmissionError("replacement tasks must be distinct public task directory names")
        if not isinstance(block.get("reason"), str) or len(block["reason"].strip()) < 5:
            raise SubmissionError("replacement block requires a concrete reason")
        for condition, source in sources.items():
            accept_source(source)
            for task in affected:
                choices[condition, task] = source
    return choices


def _check_selection_ledger(root: Path) -> None:
    supplements = _check_supplements(root)
    ledger = _load_json_object(root / "selection_ledger.json", "selection ledger")
    choices = _ledger_choices(ledger, supplements)
    # Every declared replacement is a complete paired block, even when a later
    # block supersedes it. Interrupted attempts belong in supplemental history.
    for block in ledger["blocks"]:
        for source in block["sources"].values():
            for task in block["tasks"]:
                receipt = root / "supplements" / source / task
                _receipt_files(receipt)
                try:
                    validate_episode_manifest(receipt)
                except ValueError as error:
                    raise SubmissionError(f"replacement receipt is incomplete: {source}/{task}: {error}") from error
    expected = {}
    for (condition, task), source in choices.items():
        receipt = root / "supplements" / source / task
        effective = _study_episode(root, condition, task)
        files = _receipt_files(receipt)
        if _receipt_files(effective) != files:
            raise SubmissionError(f"effective episode differs from retained original receipt: {condition}/{task}")
        expected[f"{condition}/{task}"] = {"source": source, "receipt_sha256": object_sha256(files)}
    _expect(ledger.get("selected"), expected, "selection ledger selected receipts")
    if set(ledger["selected"]) != set(expected):
        raise SubmissionError("selection ledger has extra effective slots")
    actual = {path.parent for path in (root / "evidence").rglob("metrics.json")}
    if actual != {_study_episode(root, condition, task) for condition, task in choices}:
        raise SubmissionError("assembled main study must contain exactly 12 + 36 effective episodes")
    paired = _load_json_object(root / "evidence/paired/paired_summary.json", "effective paired summary")
    _expect(paired.get("budget_variable"), ledger["budget_variable"], "selection ledger budget_variable")


def assemble_study(plan_path: Path, output_root: Path, source_root: Path = ROOT) -> dict:
    """Build a new effective view; never edit an original trial or run a model."""
    plan = _load_json_object(plan_path, "assembly plan")
    _expect(plan.get("format_version"), 1, "assembly plan version")
    if set(plan) - {"format_version", "baseline", "conditions", "condition_order",
                    "baseline_replacements", "replacements", "budget_variable"}:
        raise SubmissionError("unknown assembly plan fields")
    conditions = plan.get("conditions")
    if not isinstance(conditions, dict):
        raise SubmissionError("assembly plan requires named condition roots")
    labels = plan.get("condition_order", list(conditions))
    if (not isinstance(labels, list) or any(not isinstance(label, str) for label in labels)
            or len(labels) != len(conditions) or set(labels) != set(conditions)):
        raise SubmissionError("condition_order must list each condition exactly once")
    sources = {"original-baseline": plan.get("baseline")}
    initial = {"baseline": "original-baseline"}
    for label, path in conditions.items():
        _require_label(label)
        source = f"original-{label}"
        initial[label] = source
        sources[source] = path
    blocks = []
    for key, kind in (("baseline_replacements", "baseline"), ("replacements", "paired")):
        values = plan.get(key, [])
        if not isinstance(values, list):
            raise SubmissionError(f"{key} must be a list")
        for index, block in enumerate(values, 1):
            if not isinstance(block, dict):
                raise SubmissionError("replacement block must be an object")
            if set(block) != {"tasks", "reason", "root" if kind == "baseline" else "conditions"}:
                raise SubmissionError("replacement block requires tasks, reason, and its source roots")
            roots = {"baseline": block.get("root")} if kind == "baseline" else block.get("conditions")
            if not isinstance(roots, dict):
                raise SubmissionError("paired replacement requires condition roots")
            names = {}
            for label, path in roots.items():
                _require_label(label)
                name = f"{kind}-replacement-{index:03d}-{label}"
                names[label] = name
                sources[name] = path
            blocks.append({"kind": kind, "tasks": block.get("tasks"), "sources": names,
                           "reason": block.get("reason")})
    ledger = {
        "format_version": 1, "condition_order": labels,
        "task_order": [task.name for task in discover_tasks(ROOT / "tasks/dev")],
        "budget_variable": plan.get("budget_variable", False), "initial": initial, "blocks": blocks,
    }
    if output_root.exists():
        raise SubmissionError("assembly output must be a new directory")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hw2-assemble-", dir=output_root.parent) as temporary:
        stage = Path(temporary) / "study"
        stage.mkdir()
        supplemental = {}
        for name, value in sources.items():
            if not isinstance(value, str) or not value:
                raise SubmissionError(f"missing source directory: {name}")
            source = Path(value).expanduser()
            if not source.is_absolute():
                source = plan_path.parent / source
            if source.resolve() == output_root.resolve() or source.resolve() in output_root.resolve().parents:
                raise SubmissionError("assembly output must be outside every input evidence directory")
            supplemental[name] = _copy_supplement(source, stage / "supplements" / name)
            _check_file_limits(stage)
        choices = _ledger_choices(ledger, supplemental)
        selected = {}
        for (condition, task), source in choices.items():
            receipt = stage / "supplements" / source / task
            files = _receipt_files(receipt)
            target = _study_episode(stage, condition, task)
            for relative in files:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(receipt / relative, destination)
            run = _load_json_object(receipt / "metrics.json", "selected metrics").get("run")
            if (not isinstance(run, dict) or not isinstance(run.get("source_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", run["source_sha256"])
                    or type(run.get("seed")) is not int or run.get("backend") != "endpoint"):
                raise SubmissionError("selected episode requires a valid source identity")
            try:
                validate_episode_manifest(receipt, run)
            except ValueError as error:
                raise SubmissionError(f"invalid selected episode receipt: {condition}/{task}: {error}") from error
            snapshot = receipt.parent / "sources" / run["source_sha256"]
            destination = target.parent / "sources" / run["source_sha256"]
            if snapshot.exists() and not destination.exists():
                shutil.copytree(snapshot, destination)
            selected[f"{condition}/{task}"] = {"source": source, "receipt_sha256": object_sha256(files)}
        ledger["selected"] = selected
        write_json(stage / "selection_ledger.json", ledger)
        write_json(stage / "supplements_manifest.json", {"format_version": 1, "supplements": supplemental})
        reports = {}
        for condition in ["baseline", *labels]:
            records = []
            for task in discover_tasks(ROOT / "tasks/dev"):
                episode = _study_episode(stage, condition, task.name)
                receipt = _load_json_object(episode / "episode_manifest.json", "selected episode receipt")
                record = load_task_record(task, episode, seed=receipt["run"]["seed"],
                                          backend=receipt["run"]["backend"],
                                          runner_returncode=receipt["runner_returncode"], error=receipt["error"])
                records.append(record)
            directory = _study_episode(stage, condition, "unused").parent
            reports[condition] = write_batch_reports(
                records, directory, split="public-dev" if condition == "baseline" else f"public-dev:{condition}")
            for record in records:
                record["output_dir"] = str(output_root / Path(record["output_dir"]).relative_to(stage))
            write_json(directory / "summary.json", reports[condition])
            with (directory / "tasks.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=TASK_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(records)
        try:
            write_paired_reports({label: reports[label]["records"] for label in labels},
                                 stage / "evidence/paired", budget_variable=ledger["budget_variable"])
        except ValueError as error:
            raise SubmissionError(f"assembled paired identity/settings mismatch: {error}") from error
        # Validate with the real release and final student source, without
        # manufacturing reports, changing receipts, or calling an endpoint.
        validation = Path(temporary) / "validation"
        shutil.copytree(stage, validation)
        for relative in REQUIRED_RUNTIME_PATHS:
            _copy_source_path(source_root, source_root / relative, validation / relative)
        if (source_root / "Dockerfile").is_file():
            _copy_source_path(source_root, source_root / "Dockerfile", validation / "Dockerfile")
        _check_integrity(validation)
        _scan_for_secrets(validation)
        _reject_hidden_score_claims(validation)
        _check_required_experiment_evidence(
            validation,
            summaries=[path.relative_to(validation).as_posix() for path in (validation / "evidence").rglob("summary.json")],
            paired_summaries=["evidence/paired/paired_summary.json"],
        )
        _check_selection_ledger(validation)
        _check_file_limits(stage)
        stage.rename(output_root)
    return {"valid": True, "study": str(output_root), "effective_episodes": 48,
            "retained_roots": len(supplemental), "model_calls": 0}


def parse_result(value: str) -> tuple[str, Path]:
    label, separator, path_text = value.partition("=")
    if not separator:
        path = Path(value).expanduser().resolve()
        label = path.name
    else:
        path = Path(path_text).expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", label):
        raise SubmissionError("result labels must contain only letters, digits, '.', '_', or '-'")
    if not path.is_dir():
        raise SubmissionError(f"results directory does not exist: {path}")
    return label, path


def _check_python_include(source_root: Path, include: Path) -> None:
    """Use the same inventory as run identity; reserved/namespace roots cannot bypass it."""
    candidates = list(include.rglob("*.py")) if include.is_dir() else [include]
    try:
        inventory = source_files(source_root)
    except ValueError as error:
        raise SubmissionError(str(error)) from error
    for path in candidates:
        if path.suffix == ".py" and path.relative_to(source_root).as_posix() not in inventory:
            raise SubmissionError(
                "Python --include must be hash-backed: use a top-level .py module or a regular "
                "package with top-level __init__.py outside reserved roots; alternatively use harness/"
            )


def _copy_source_path(source_root: Path, source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise SubmissionError(f"source symlinks are not packaged: {source}")
    source = source.resolve()
    try:
        relative = source.relative_to(source_root)
    except ValueError as error:
        raise SubmissionError(f"source path is outside release root: {source}") from error
    if _has_forbidden_part(relative):
        raise SubmissionError(f"refusing to package forbidden source path: {relative}")
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise SubmissionError(
                    f"source symlinks are not packaged: {path.relative_to(source_root)}"
                )
            if not path.is_file():
                continue
            child_relative = path.relative_to(source_root)
            if _has_forbidden_part(child_relative) or path.suffix == ".pyc":
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _copy_evidence(source_root: Path, destination: Path) -> None:
    copied = 0
    exported, snapshots = set(), set()
    for manifest in source_root.rglob("episode_manifest.json"):
        payload = _load_json_object(manifest, str(manifest))
        for relative in payload.get("files", {}):
            logical = PurePosixPath(relative)
            if logical.is_absolute() or ".." in logical.parts:
                raise SubmissionError("unsafe path in episode artifact manifest")
            exported.add(manifest.parent / relative)
        run = payload.get("run")
        if isinstance(run, dict) and isinstance(run.get("source_files"), dict):
            digest = run.get("source_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SubmissionError("invalid source snapshot identity")
            for relative in run["source_files"]:
                logical = _safe_relative(relative)
                snapshots.add(manifest.parent.parent / "sources" / digest / logical)
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(source_root)
        if not source.is_file() or (
            source.name not in EVIDENCE_FILENAMES and source not in snapshots and source not in exported
        ):
            continue
        if source.is_symlink():
            raise SubmissionError(f"evidence symlinks are not packaged: {source}")
        if _has_forbidden_part(relative):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    if not copied:
        raise SubmissionError(f"no recognized result artifacts under {source_root}")


def _write_deterministic_zip(root: Path, output_path: Path) -> None:
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes())


def _validate_zip_members(infos: Iterable[zipfile.ZipInfo]) -> None:
    seen = set()
    size = 0
    for info in infos:
        _safe_relative(info.filename.rstrip("/"))
        if info.filename in seen:
            raise SubmissionError(f"duplicate archive path: {info.filename}")
        seen.add(info.filename)
        size += info.file_size
        if info.file_size > MAX_FILE_BYTES or size > MAX_PACKAGE_BYTES or len(seen) > MAX_PACKAGE_FILES:
            raise SubmissionError("archive exceeds expanded file/count/size limits")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
            raise SubmissionError(f"archive must contain regular files/directories: {info.filename}")


def _reject_forbidden_paths(files: list[str]) -> None:
    for filename in files:
        path = PurePosixPath(filename)
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            raise SubmissionError(f"forbidden path in submission: {filename}")
        if path.name in FORBIDDEN_FILENAMES or path.suffix.lower() in {".pem", ".key"}:
            raise SubmissionError(f"credential-like file in submission: {filename}")


def _check_integrity(root: Path, *, source_snapshot: bool = False) -> None:
    expected = json.loads(INTEGRITY_MANIFEST.read_text(encoding="utf-8"))
    for relative, digest in expected["sha256"].items():
        # Configs are packaged and identity-bound separately from source
        # snapshots. The complete package always checks every protected path.
        if source_snapshot and PurePosixPath(relative).parts[0] not in SOURCE_PATHS:
            continue
        path = root / relative
        if not path.is_file() or _sha256(path) != digest:
            raise SubmissionError(f"staff-owned file was modified: {relative}")


def _check_manifest(root: Path) -> None:
    manifest_path = root / "submission_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SubmissionError("submission_manifest.json is invalid") from error
    if not isinstance(manifest, dict):
        raise SubmissionError("submission_manifest.json must contain an object")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise SubmissionError("submission manifest is missing file hashes")
    actual = _file_hashes(root, exclude={"submission_manifest.json"})
    expected_without_manifest = {
        path: digest
        for path, digest in expected.items()
        if path != "submission_manifest.json"
    }
    if actual != expected_without_manifest:
        raise SubmissionError("submission file hashes do not match the manifest")
    claims = manifest.get("claims", {})
    if not isinstance(claims, dict):
        raise SubmissionError("submission manifest claims must be an object")
    if claims.get("contains_hidden_score") is not False:
        raise SubmissionError("submission manifest may not claim a hidden score")
    if claims.get("contains_private_or_final_tasks") is not False:
        raise SubmissionError("private or final task data may not be submitted")


def _scan_for_secrets(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() == ".pdf":
            continue
        try:
            text = path.relative_to(root).as_posix() + "\n" + path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                # Public localhost placeholder shipped by the model client; it
                # is not a course or personal credential.
                if match.lastindex == 2 and match.group(2) in {
                    "local-development-only", "[REDACTED]", "<REDACTED>",
                }:
                    continue
                raise SubmissionError(
                    f"possible hardcoded credential in {path.relative_to(root)}"
                )


def _reject_hidden_score_claims(root: Path) -> None:
    for path in root.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        hits = sorted(_json_keys(payload) & FORBIDDEN_CLAIM_KEYS)
        if hits:
            raise SubmissionError(
                f"self-reported private/final score fields in {path.relative_to(root)}: {hits}"
            )


def _json_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = {str(key).lower() for key in value}
        for item in value.values():
            keys.update(_json_keys(item))
        return keys
    if isinstance(value, list):
        keys = set()
        for item in value:
            keys.update(_json_keys(item))
        return keys
    return set()


def _require_completed_text(path: Path, label: str) -> None:
    _require_file(path, label)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SubmissionError(f"{label} must be UTF-8 text") from error
    if len(text.strip()) < 20:
        raise SubmissionError(f"{label} is incomplete")
    if any(pattern.search(text) for pattern in PLACEHOLDER_PATTERNS):
        raise SubmissionError(f"{label} still contains template placeholders")


def _require_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SubmissionError(f"{label} does not exist or is not a regular file: {path}")


def _file_hashes(root: Path, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in excluded
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_forbidden_part(path: Path) -> bool:
    return any(part in FORBIDDEN_PARTS for part in path.parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    package_parser = subparsers.add_parser("package")
    package_parser.add_argument("--source-root", default=str(ROOT))
    package_parser.add_argument("--output", required=True)
    package_parser.add_argument("--report", required=True)
    package_parser.add_argument("--disclosure", default=str(ROOT / "ai_disclosure.md"))
    package_parser.add_argument("--reproduction", default=str(ROOT / "REPRODUCE.md"))
    package_parser.add_argument("--results", action="append", default=[])
    package_parser.add_argument("--study", type=Path, help="Effective study produced by assemble")
    package_parser.add_argument("--supplement", action="append", default=[],
                                help="NAME=PATH for unscored partial/probe/original history")
    package_parser.add_argument("--include", action="append", default=[])

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("archive")
    reverify_parser = subparsers.add_parser("reverify")
    reverify_parser.add_argument("archive")
    reverify_parser.add_argument("--output", required=True)
    assemble_parser = subparsers.add_parser(
        "assemble", help="Create a complete effective study from retained original and replacement receipts")
    assemble_parser.add_argument("--plan", required=True, type=Path,
                                 help="Version-1 JSON plan; source paths are relative to this file")
    assemble_parser.add_argument("--output", required=True, type=Path,
                                 help="New directory outside all input evidence directories")
    assemble_parser.add_argument("--source-root", default=ROOT, type=Path)

    args = parser.parse_args()
    try:
        if args.command == "package":
            source_root = Path(args.source_root).resolve()
            results = [parse_result(value) for value in args.results]
            payload = package_submission(
                source_root=source_root,
                output_path=Path(args.output).expanduser().resolve(),
                report_path=Path(args.report).expanduser().resolve(),
                disclosure_path=Path(args.disclosure).expanduser().resolve(),
                reproduction_path=Path(args.reproduction).expanduser().resolve(),
                results=results,
                includes=[
                    source_root / value
                    for value in args.include
                ],
                supplements=[parse_result(value) for value in args.supplement],
                study=args.study.expanduser().resolve() if args.study else None,
            )
        elif args.command == "assemble":
            payload = assemble_study(args.plan.expanduser().resolve(), args.output.expanduser().resolve(),
                                     args.source_root.expanduser().resolve())
        elif args.command == "reverify":
            payload = reverify_submission(Path(args.archive).expanduser().resolve(),
                                          Path(args.output).expanduser().resolve())
        else:
            payload = check_submission(Path(args.archive).expanduser().resolve())
    except SubmissionError as error:
        raise SystemExit(f"submission check failed: {error}") from error
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
