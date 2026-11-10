"""Required HW2 command-line entry point."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from harness.agent import RepairAgent
from harness.budget import Budget
from harness.model_client import (
    OpenAICompatibleModelClient, ReplayModelClient, resolve_model_config,
    sanitize, write_json,
)
from harness.patch import git_diff
from harness.state import AgentState
from harness.sandbox import Sandbox
from eval.run_identity import build_run_identity, sha256


OUTPUT_MARKER = ".cs8803_hw2_output"
ROOT = Path(__file__).resolve().parent
HARD_CAPS = {
    "max_model_calls": 12,
    "max_total_tokens": 120_000,
    "max_active_seconds": 1800.0,
}


def initialize_worktree(task_dir: Path, output_dir: Path) -> Path:
    worktree = output_dir / "worktree"
    shutil.copytree(task_dir / "repo", worktree, symlinks=True)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Course Agent"], cwd=worktree, check=True)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)
    return worktree


def prepare_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        marker = output_dir / OUTPUT_MARKER
        entries = list(output_dir.iterdir())
        if entries == [marker] and marker.is_file():
            marker.unlink()
        elif entries:
            raise SystemExit(
                f"Refusing to overwrite nonempty output directory: {output_dir}"
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / OUTPUT_MARKER).write_text("managed by run_agent.py\n", encoding="utf-8")


def validate_directories(task_dir: Path, output_dir: Path) -> None:
    repository = (task_dir / "repo").resolve()
    if not (task_dir / "task.json").is_file() or not repository.is_dir():
        raise SystemExit(f"Invalid task directory: {task_dir}")
    if output_dir == repository or repository in output_dir.parents:
        raise SystemExit("Output directory must be outside the task repository")


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise SystemExit("Harness config must be a JSON object")
    for name, hard_cap in HARD_CAPS.items():
        value = config.get(name, hard_cap)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SystemExit(f"Config field {name} must be numeric")
        if not math.isfinite(value):
            raise SystemExit(f"Config field {name} must be finite")
        if name in {"max_model_calls", "max_total_tokens"} and not isinstance(value, int):
            raise SystemExit(f"Config field {name} must be an integer")
        if value <= 0 or value > hard_cap:
            raise SystemExit(f"Config field {name} must be in (0, {hard_cap}]")
    temperature = config.get("temperature", 0.2)
    top_p = config.get("top_p", 1.0)
    max_output_tokens = config.get("max_output_tokens", 4096)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        raise SystemExit("temperature must be between 0 and 1")
    if isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not 0 < top_p <= 1:
        raise SystemExit("top_p must be in (0, 1]")
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens <= 0
        or max_output_tokens > 4096
    ):
        raise SystemExit("max_output_tokens must be an integer in [1, 4096]")
    for name in ("bootstrap_context", "auto_test_after_write"):
        if not isinstance(config.get(name, False), bool):
            raise SystemExit(f"Config field {name} must be boolean")
    return config


def config_sha256(path: Path) -> str:
    return sha256(path)


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact(item, secrets) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--backend", choices=("replay", "endpoint"), default="endpoint")
    parser.add_argument("--config", default=str(ROOT / "config" / "default.json"))
    args = parser.parse_args()

    task_dir = Path(args.task_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    validate_directories(task_dir, output_dir)
    try:
        model_config = resolve_model_config() if args.backend == "endpoint" else None
        identity = build_run_identity(
            task_dir=task_dir, seed=args.seed, backend=args.backend,
            config=config_path, model_card=ROOT / "MODEL_CARD.md",
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    prepare_output_dir(output_dir)
    (output_dir / "api_attempts.jsonl").touch()
    write_json(output_dir / "INCOMPLETE.json", {
        "run": identity, "status": "running",
        "recovery": "Preserve this trial; never overwrite it or silently discard a failed request.",
    })

    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    budget = Budget(
        max_model_calls=int(config.get("max_model_calls", HARD_CAPS["max_model_calls"])),
        max_tokens=int(config.get("max_total_tokens", HARD_CAPS["max_total_tokens"])),
        max_active_seconds=float(
            config.get("max_active_seconds", HARD_CAPS["max_active_seconds"])
        ),
    )
    client = (
        ReplayModelClient(task_dir / "replay.json")
        if args.backend == "replay"
        else OpenAICompatibleModelClient(
            budget=budget, output_dir=output_dir, config=model_config,
            temperature=float(config.get("temperature", 0.2)),
            top_p=float(config.get("top_p", 1.0)),
            max_output_tokens=int(config.get("max_output_tokens", 4096)),
        )
    )
    scratch = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-work-", dir=output_dir.parent))
    worktree = scratch / "worktree"
    state = None
    agent = None
    patch = ""
    export_failed = False
    try:
        with Sandbox(worktree):
            initialize_worktree(task_dir, scratch)
            # Setup precedes the episode; all model/tool/API waits are timed.
            budget.started_at = time.monotonic()
            agent = RepairAgent(
                client, budget, args.seed,
                bootstrap_context=bool(config.get("bootstrap_context", False)),
                auto_test_after_write=bool(config.get("auto_test_after_write", False)),
            )
            state = agent.run(
                task["issue"], worktree,
                required_test_command=task.get("visible_command", "python -m pytest -q"),
            )
            patch = git_diff(worktree)
    except (Exception, KeyboardInterrupt) as error:
        # Keep the only repaired worktree until a valid patch and evidence exist.
        export_failed = True
        if state is None:
            state = getattr(agent, "state", None)
        available = state is not None
        if state is None:
            state = AgentState(issue=task["issue"])
        state.status = "interrupted" if isinstance(error, KeyboardInterrupt) else "error"
        state.record("budget", **budget.metrics())
        state.record("error", step=state.step, phase="execution_or_patch_export",
                     message=f"{type(error).__name__}: {error}", agent_state_available=available)
        state.record("state", status=state.status, step=state.step)
    metrics = budget.metrics()
    key = model_config.api_key if model_config else ""
    (output_dir / "patch.diff").write_text(sanitize(patch, key), encoding="utf-8")
    secrets = tuple(
        value
        for name in ("HW2_MODEL_API_KEY", "COURSE_MODEL_API_KEY")
        if (value := os.environ.get(name))
    )
    with (output_dir / "trajectory.jsonl").open("w", encoding="utf-8") as handle:
        for event in state.events:
            handle.write(json.dumps(sanitize(redact(event.to_dict(), secrets), key), sort_keys=True) + "\n")
    metrics["run"] = identity
    metrics["termination_reason"] = state.status
    metrics["visible_tests_passed"] = state.visible_tests_passed
    metrics["patch_nonempty"] = bool(patch.strip())
    metrics["resolved"] = bool(patch.strip()) and state.visible_tests_passed is True
    provider = getattr(client, "stats", {
        "logical_calls": budget.model_calls, "http_attempts": 0, "retries": 0,
        "http_429": 0, "http_5xx": 0, "unknown_usage_attempts": 0,
        "request_seconds": 0.0, "provider_wait_seconds": 0.0,
        "pacing_wait_seconds": 0.0, "provider_requested_wait_seconds": 0.0,
        "reported_reasoning_tokens": 0, "reasoning_usage_reported_attempts": 0,
        "usage_accounting_complete": True, "reserved_unknown_tokens": 0,
        "reported_total_tokens": budget.total_tokens, "returned_models": ["deterministic-replay"],
    })
    metrics["provider"] = provider
    metrics["token_cap_certified"] = (
        provider["usage_accounting_complete"] and budget.total_tokens <= budget.max_tokens
    )
    metrics["api_latency_and_retry_wait_exempted"] = False
    write_json(output_dir / "metrics.json", metrics, key)
    write_json(output_dir / "provider_metrics.json", provider, key)
    write_json(output_dir / "final_state.json", redact(state.to_dict(), secrets), key)
    if export_failed:
        write_json(output_dir / "RECOVERY.json", {
            "status": "patch_export_or_execution_failed", "worktree": str(worktree),
            "recovery": "Worktree retained. Inspect it and recover the patch under Sandbox; do not execute its code directly. Never overwrite this failed trial.",
        }, key)
    else:
        try:
            shutil.rmtree(scratch)
        except OSError:
            write_json(output_dir / "RECOVERY.json", {
                "status": "cleanup_pending", "worktree": str(worktree),
                "recovery": "Patch and logs are saved. Remove this scratch directory after reviewing the evidence.",
            }, key)
    if state.status == "interrupted":
        write_json(output_dir / "INCOMPLETE.json", {
            "run": identity, "status": "interrupted",
            "recovery": "Evidence retained. Start another trial in a new directory.",
        })
        raise SystemExit(130)
    (output_dir / "INCOMPLETE.json").unlink()
    (output_dir / OUTPUT_MARKER).unlink()
    if state.status == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
