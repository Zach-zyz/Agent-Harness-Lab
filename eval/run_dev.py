"""Run a deterministic ordered batch of public development tasks."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .report import load_task_record, write_batch_reports, write_json
from .run_identity import build_run_identity, execution_identity, sha256
from .validate_patch import validate_patch


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_EPISODE_FILES = (
    "patch.diff",
    "trajectory.jsonl",
    "metrics.json",
    "final_state.json",
    "provider_metrics.json",
    "api_attempts.jsonl",
)
REQUIRED_METRIC_NUMBERS = (
    "active_seconds",
    "input_tokens",
    "max_active_seconds",
    "max_model_calls",
    "max_tokens",
    "model_calls",
    "output_tokens",
    "test_invocations",
    "test_seconds",
    "total_tokens",
)
FINAL_STATE_FIELDS = (
    "issue",
    "step",
    "status",
    "summary",
    "last_observation",
    "visible_tests_passed",
)
SUCCESSFUL_TERMINATIONS = {"finished", "budget_exhausted", "error", "interrupted"}


def episode_hashes(output_dir: Path) -> dict[str, str]:
    """Hash every exported episode artifact, never transient worktrees."""
    files = {}
    for path in sorted(output_dir.rglob("*")):
        relative = path.relative_to(output_dir)
        if relative.parts[0] in {"worktree", ".cs8803_hw2_output"}:
            continue
        if path.is_symlink():
            raise ValueError(f"episode artifact symlink: {relative}")
        if path.is_file() and relative.as_posix() != "episode_manifest.json":
            files[relative.as_posix()] = sha256(path)
    return files


def write_episode_manifest(output_dir: Path, run: dict, returncode: int, error: str) -> None:
    write_json(output_dir / "episode_manifest.json", {
        "format_version": 1, "run": run, "runner_returncode": returncode,
        "error": error, "files": episode_hashes(output_dir),
    })


def validate_episode_manifest(output_dir: Path, expected_run: dict | None = None) -> dict:
    try:
        manifest = json.loads((output_dir / "episode_manifest.json").read_text())
        if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
            raise ValueError("invalid episode_manifest.json schema")
        if manifest.get("files") != episode_hashes(output_dir):
            raise ValueError("episode_manifest.json artifact hashes mismatch")
        if expected_run is not None and manifest.get("run") != expected_run:
            raise ValueError("episode_manifest.json run identity mismatch")
        if type(manifest.get("runner_returncode")) is not int or not isinstance(manifest.get("error"), str):
            raise ValueError("episode_manifest.json runner outcome missing")
        return manifest
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("cannot read episode_manifest.json") from error


def snapshot_source(output_root: Path, identity: dict, source_root: Path = ROOT) -> None:
    destination = output_root / "sources" / identity["source_sha256"]
    for relative, digest in identity["source_files"].items():
        target = destination / relative
        if target.exists():
            if target.is_symlink() or sha256(target) != digest:
                raise ValueError(f"source snapshot changed: {relative}")
            continue
        source = source_root / relative
        if sha256(source) != digest:
            raise ValueError(f"source changed while snapshotting: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def discover_tasks(task_root: Path, selected: list[str] | None = None) -> list[Path]:
    if not task_root.is_dir():
        raise ValueError(f"task root does not exist: {task_root}")
    tasks = [
        path
        for path in sorted(task_root.iterdir(), key=lambda item: item.name)
        if path.is_dir() and (path / "task.json").is_file() and (path / "repo").is_dir()
    ]
    if selected:
        requested = set(selected)
        tasks = [
            task
            for task in tasks
            if task.name in requested or _task_id(task) in requested
        ]
        found = {task.name for task in tasks} | {_task_id(task) for task in tasks}
        missing = sorted(requested - found)
        if missing:
            raise ValueError(f"unknown task selections: {', '.join(missing)}")
    if not tasks:
        raise ValueError(f"no tasks found under {task_root}")
    return tasks


def run_batch(
    *,
    task_root: Path,
    output_root: Path,
    backend: str,
    seed: int,
    config: Path,
    selected: list[str] | None = None,
    resume: bool = False,
    fail_fast: bool = False,
    split: str = "public-dev",
) -> dict[str, Any]:
    tasks = discover_tasks(task_root, selected)
    output_root.mkdir(parents=True, exist_ok=True)
    existing = [
        output_root / task.name
        for task in tasks
        if (output_root / task.name).exists()
    ]
    if existing and not resume:
        names = ", ".join(path.name for path in existing)
        raise ValueError(
            f"episode output already exists for {names}; use a new output root or --resume"
        )
    records: list[dict[str, Any]] = []
    for task_dir in tasks:
        output_dir = output_root / task_dir.name
        returncode = 0
        error = ""
        expected_run = build_run_identity(
            task_dir=task_dir,
            seed=seed,
            backend=backend,
            config=config,
            model_card=ROOT / "MODEL_CARD.md",
        )
        complete = all((output_dir / name).is_file() for name in REQUIRED_EPISODE_FILES)
        if resume and output_dir.exists() and not complete:
            raise ValueError(
                f"cannot resume incomplete episode output for {task_dir.name}; "
                "use a new output root or remove the incomplete directory after inspection"
            )
        if resume and complete:
            saved = validate_episode_manifest(output_dir)
            if not isinstance(saved.get("run"), dict) or (
                execution_identity(saved["run"]) != execution_identity(expected_run)
            ):
                raise ValueError("episode_manifest.json run identity mismatch")
            # Keep the original runtime metadata in every saved artifact. Only
            # matching against the current host tolerates patch/OS updates.
            expected_run = saved["run"]
            returncode, error = saved["runner_returncode"], saved["error"]
            artifact_error = validate_episode_artifacts(
                output_dir,
                expected_run=expected_run,
            )
            if artifact_error:
                raise ValueError(
                    f"cannot resume invalid episode output for {task_dir.name}: "
                    f"{artifact_error}"
                )
            _validate_resume_metadata(
                output_dir,
                task_dir=task_dir,
                seed=seed,
                backend=backend,
                config=config,
            )
        else:
            snapshot_source(output_root, expected_run)
            command = [
                sys.executable,
                str(ROOT / "run_agent.py"),
                "--task_dir",
                str(task_dir),
                "--output_dir",
                str(output_dir),
                "--seed",
                str(seed),
                "--backend",
                backend,
                "--config",
                str(config),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            returncode = completed.returncode
            if returncode:
                error = _bounded_error(completed.stdout, completed.stderr)

        artifact_error = validate_episode_artifacts(
            output_dir,
            expected_run=expected_run,
        )
        if artifact_error and not error:
            error = artifact_error
        if not artifact_error and not error:
            metrics = json.loads((output_dir / "metrics.json").read_text())
            if metrics["termination_reason"] in {"error", "interrupted"}:
                error = f"run_agent terminated with {metrics['termination_reason']}"

        patch_path = output_dir / "patch.diff"
        if patch_path.is_file():
            previous = output_dir / "verification.json"
            if resume and previous.is_file():
                history = output_dir / "verification_history" / f"{sha256(previous)}.json"
                history.parent.mkdir(parents=True, exist_ok=True)
                if not history.exists():
                    shutil.copyfile(previous, history)
            try:
                verification = validate_patch(task_dir, patch_path)
            except Exception as failure:
                error = error or f"verification failed: {_bounded_error('', str(failure))}"
                verification = {
                    "resolved": False, "patch_valid": False, "f2p_passed": False,
                    "p2p_passed": False, "protected_paths_clean": False,
                    "details": {"header_paths": [], "patch_error": error},
                }
            verification["patch_sha256"] = sha256(patch_path)
            verification["task_input_sha256"] = expected_run["task_input_sha256"]
            write_json(output_dir / "verification.json", verification)
        elif not error:
            error = "run_agent did not produce patch.diff"

        if output_dir.is_dir():
            write_episode_manifest(output_dir, expected_run, returncode, error)
        record = load_task_record(
            task_dir,
            output_dir,
            seed=seed,
            backend=backend,
            runner_returncode=returncode,
            error=error,
        )
        records.append(record)
        print(
            f"{record['task_id']}: "
            f"{'resolved' if record['resolved'] else 'unresolved'} "
            f"(calls={record['model_calls']}, tokens={record['total_tokens']})"
        )
        if fail_fast and (returncode or not record["resolved"]):
            break

    return write_batch_reports(records, output_root, split=split)


def _validate_resume_metadata(
    output_dir: Path,
    *,
    task_dir: Path,
    seed: int,
    backend: str,
    config: Path,
) -> None:
    try:
        metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read resume metadata in {output_dir}") from error
    actual = metrics.get("run") if isinstance(metrics, dict) else None
    expected = build_run_identity(
        task_dir=task_dir,
        seed=seed,
        backend=backend,
        config=config,
        model_card=ROOT / "MODEL_CARD.md",
    )
    if not isinstance(actual, dict) or execution_identity(actual) != execution_identity(expected):
        raise ValueError(
            f"resume metadata mismatch in {output_dir}: inputs/config/source/provider/runtime changed"
        )


def validate_episode_artifacts(
    output_dir: Path,
    *,
    expected_run: dict[str, Any] | None = None,
) -> str:
    for name in REQUIRED_EPISODE_FILES:
        path = output_dir / name
        if not path.is_file() or path.is_symlink():
            return f"run_agent did not produce a regular {name}"

    try:
        (output_dir / "patch.diff").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "patch.diff is not readable UTF-8 text"

    try:
        event_count = 0
        for line_number, line in enumerate(
            (output_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                return f"trajectory.jsonl line {line_number} is not a JSON object"
            event_count += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return f"trajectory.jsonl is invalid: {error}"
    if event_count == 0:
        return "trajectory.jsonl must contain at least one event"

    payloads = {}
    for name in ("metrics.json", "final_state.json"):
        try:
            payload = json.loads((output_dir / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return f"{name} is invalid: {error}"
        if not isinstance(payload, dict):
            return f"{name} must contain a JSON object"
        payloads[name] = payload

    metrics = payloads["metrics.json"]
    final_state = payloads["final_state.json"]
    termination = metrics.get("termination_reason")
    if termination not in SUCCESSFUL_TERMINATIONS:
        return f"run_agent terminated with invalid status: {termination!r}"
    for name in REQUIRED_METRIC_NUMBERS:
        value = metrics.get(name)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            return f"metrics.json field {name} must be a nonnegative number"
    for name in ("model_calls", "input_tokens", "output_tokens", "test_invocations"):
        if not isinstance(metrics[name], int):
            return f"metrics.json field {name} must be an integer"
    tool_calls = metrics.get("tool_calls")
    if not isinstance(tool_calls, dict) or any(
        not isinstance(name, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for name, count in tool_calls.items()
    ):
        return "metrics.json field tool_calls must map names to nonnegative integers"
    if metrics["total_tokens"] != metrics["input_tokens"] + metrics["output_tokens"]:
        return "metrics.json total_tokens is inconsistent"
    # Accounting is updated before BudgetExceeded is raised. A completed
    # budget-exhausted trial may therefore record an over-limit last operation;
    # retain it as failed experiment evidence instead of aborting the batch.
    # This does not grant extra budget or certify the patch for hidden grading.
    if termination not in {"budget_exhausted", "error", "interrupted"}:
        if metrics["model_calls"] > metrics["max_model_calls"]:
            return "metrics.json model_calls exceeds max_model_calls"
        if metrics["total_tokens"] > metrics["max_tokens"]:
            return "metrics.json total_tokens exceeds max_tokens"
        if metrics["active_seconds"] > metrics["max_active_seconds"]:
            return "metrics.json active_seconds exceeds max_active_seconds"

    visible = metrics.get("visible_tests_passed")
    if visible is not None and not isinstance(visible, bool):
        return "metrics.json visible_tests_passed must be boolean or null"
    patch_nonempty = metrics.get("patch_nonempty")
    resolved = metrics.get("resolved")
    if not isinstance(patch_nonempty, bool) or not isinstance(resolved, bool):
        return "metrics.json patch_nonempty and resolved must be boolean"
    actual_patch_nonempty = bool(
        (output_dir / "patch.diff").read_text(encoding="utf-8").strip()
    )
    if patch_nonempty != actual_patch_nonempty:
        return "metrics.json patch_nonempty is inconsistent"
    if resolved != (patch_nonempty and visible is True):
        return "metrics.json resolved is inconsistent"

    actual_run = metrics.get("run")
    if not isinstance(actual_run, dict):
        return "metrics.json run identity must be a JSON object"
    if expected_run is not None and actual_run != expected_run:
        return "metrics.json run identity mismatch"
    try:
        provider = json.loads((output_dir / "provider_metrics.json").read_text())
    except (OSError, UnicodeError, ValueError):
        return "provider_metrics.json is invalid"
    if not isinstance(provider, dict) or metrics.get("provider") != provider:
        return "provider_metrics.json does not match metrics provider"
    if type(provider.get("usage_accounting_complete")) is not bool:
        return "provider usage_accounting_complete must be boolean"
    if metrics.get("token_cap_certified") is not (
        provider["usage_accounting_complete"] and metrics["total_tokens"] <= metrics["max_tokens"]
    ):
        return "metrics.json token_cap_certified is inconsistent"
    if metrics.get("api_latency_and_retry_wait_exempted") is not False:
        return "metrics.json must count API latency and retry wait in active time"

    missing_final = [name for name in FINAL_STATE_FIELDS if name not in final_state]
    if missing_final:
        return "final_state.json missing fields: " + ", ".join(missing_final)
    if (
        not isinstance(final_state["issue"], str)
        or not isinstance(final_state["step"], int)
        or isinstance(final_state["step"], bool)
        or final_state["step"] < 0
        or not isinstance(final_state["status"], str)
        or not isinstance(final_state["summary"], str)
        or not isinstance(final_state["last_observation"], str)
        or (
            final_state["visible_tests_passed"] is not None
            and not isinstance(final_state["visible_tests_passed"], bool)
        )
    ):
        return "final_state.json has invalid field types"
    if final_state["status"] != termination:
        return "final_state.json status does not match metrics termination_reason"
    if final_state["visible_tests_passed"] != visible:
        return "final_state.json visible_tests_passed does not match metrics"
    return ""


def _task_id(task_dir: Path) -> str:
    try:
        payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return task_dir.name
    return payload.get("id", task_dir.name) if isinstance(payload, dict) else task_dir.name


def _bounded_error(stdout: str, stderr: str) -> str:
    from harness.model_client import sanitize
    combined = (stdout + "\n" + stderr).strip()
    return sanitize(combined)[-4000:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task_root", default=str(ROOT / "tasks" / "dev"))
    parser.add_argument("--output_root", required=True)
    parser.add_argument("--backend", choices=("endpoint", "replay"), default="endpoint")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--config", default=str(ROOT / "config" / "default.json"))
    parser.add_argument("--task", action="append", dest="tasks")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    try:
        summary = run_batch(
            task_root=Path(args.task_root).resolve(),
            output_root=Path(args.output_root).resolve(),
            backend=args.backend,
            seed=args.seed,
            config=Path(args.config).resolve(),
            selected=args.tasks,
            resume=args.resume,
            fail_fast=args.fail_fast,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}, indent=2))
    if summary["runner_failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
