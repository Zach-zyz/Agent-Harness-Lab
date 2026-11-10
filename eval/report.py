"""Reporting helpers for batch and paired HW2 experiments."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

from .run_identity import comparison_identity, sha256
from .validate_patch import _test_report_error

TASK_COLUMNS = (
    "task_id",
    "task_name",
    "seed",
    "backend",
    "runner_returncode",
    "resolved",
    "patch_valid",
    "f2p_passed",
    "p2p_passed",
    "visible_tests_passed",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "active_seconds",
    "test_invocations",
    "test_seconds",
    "termination_reason",
    "outcome",
    "token_cap_certified",
    "usage_accounting_complete",
    "output_dir",
    "error",
)


def json_safe(value: Any) -> Any:
    """Convert subprocess diagnostics and paths into deterministic JSON values."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(
            json_safe(payload),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        raise ValueError("total must be positive")
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return center - radius, center + radius


def budget_compliant(metrics: dict[str, Any]) -> bool:
    """A passing patch earns resolution only within its recorded and hard caps."""
    provider = metrics.get("provider", {})
    if not isinstance(provider, dict):
        return False
    if metrics.get("token_cap_certified") is False or provider.get(
        "usage_accounting_complete"
    ) is False:
        return False
    for usage_name, limit_name, hard_limit in (
        ("model_calls", "max_model_calls", 12),
        ("total_tokens", "max_tokens", 120_000),
        ("active_seconds", "max_active_seconds", 1800.0),
    ):
        usage, limit = metrics.get(usage_name), metrics.get(limit_name)
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in (usage, limit)
        ):
            return False
        if not 0 < limit <= hard_limit or not 0 <= usage <= limit:
            return False
    return True


def verified_resolution(verification: dict, task: dict, patch: Path) -> bool:
    """Missing/incomplete verification can never become a successful result."""
    if any(verification.get(key) is not True for key in
           ("resolved", "patch_valid", "f2p_passed", "p2p_passed", "protected_paths_clean")):
        return False
    if not patch.is_file() or verification.get("patch_sha256") != sha256(patch):
        return False
    details = verification.get("details")
    if not isinstance(details, dict):
        return False
    for suite, field in (("f2p", "fail_to_pass"), ("p2p", "pass_to_pass")):
        result = details.get(suite, {})
        requested = task.get(field)
        if not isinstance(result, dict) or not requested or result.get("returncode") != 0 or result.get("process_returncode") != 0:
            return False
        if result.get("report_error") or _test_report_error(
            result.get("test_report"), requested, result["process_returncode"]
        ):
            return False
    return True


def load_task_record(
    task_dir: Path,
    output_dir: Path,
    *,
    seed: int,
    backend: str,
    runner_returncode: int,
    error: str = "",
) -> dict[str, Any]:
    task = _read_json(task_dir / "task.json", {})
    metrics = _read_json(output_dir / "metrics.json", {})
    verification = _read_json(output_dir / "verification.json", {})
    provider = _read_json(output_dir / "provider_metrics.json", {})
    failure = bool(runner_returncode or error or metrics.get("termination_reason") in {"error", "interrupted"})
    certified = metrics.get("token_cap_certified") is True
    resolved = bool(
        verified_resolution(verification, task, output_dir / "patch.diff")
        and budget_compliant(metrics) and not failure
        and (backend == "replay" or (
            certified and provider.get("usage_accounting_complete") is True
            and provider == metrics.get("provider")
        ))
    )
    return {
        "task_id": task.get("id", task_dir.name),
        "task_name": task_dir.name,
        "seed": seed,
        "backend": backend,
        "runner_returncode": runner_returncode,
        "resolved": resolved,
        "outcome": "error" if failure else "resolved" if resolved else "unresolved",
        "run": metrics.get("run", {}),
        "provider": provider,
        "token_cap_certified": certified,
        "usage_accounting_complete": provider.get("usage_accounting_complete") is True,
        "budget": {name: metrics.get(name) for name in
                   ("max_model_calls", "max_tokens", "max_active_seconds")},
        "patch_valid": bool(verification.get("patch_valid", False)),
        "f2p_passed": bool(verification.get("f2p_passed", False)),
        "p2p_passed": bool(verification.get("p2p_passed", False)),
        "visible_tests_passed": metrics.get("visible_tests_passed"),
        **{field: _reported_number(metrics.get(field)) for field in (
            "model_calls", "input_tokens", "output_tokens", "total_tokens",
            "active_seconds", "test_invocations", "test_seconds",
        )},
        "termination_reason": metrics.get("termination_reason", "runner_error"),
        "output_dir": str(output_dir),
        "error": error,
    }


def write_batch_reports(
    records: list[dict[str, Any]],
    output_root: Path,
    *,
    split: str,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    resolved = sum(bool(record["resolved"]) for record in records)
    total = len(records)
    interval = wilson_interval(resolved, total) if total else (0.0, 0.0)
    summary = {
        "split": split,
        "tasks": total,
        "resolved": resolved,
        "runner_failures": sum(
            bool(record["runner_returncode"] or record["error"] or record.get("outcome") == "error")
            for record in records
        ),
        "valid_unresolved": sum(record.get("outcome") == "unresolved" for record in records),
        "usage_uncertified": sum(record.get("usage_accounting_complete") is not True for record in records),
        "resolve_rate": resolved / total if total else 0.0,
        "resolve_rate_wilson_95": {"low": interval[0], "high": interval[1]},
        "cost": {
            field: _numeric_summary(records, field)
            for field in (
                "model_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "active_seconds",
                "test_invocations",
                "test_seconds",
            )
        },
        "task_order": [record["task_id"] for record in records],
        "records": records,
    }
    write_json(output_root / "summary.json", summary)
    with (output_root / "tasks.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TASK_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    (output_root / "failure_index.md").write_text(
        render_failure_index(records, output_root),
        encoding="utf-8",
    )
    return summary


def render_failure_index(records: list[dict[str, Any]], output_root: Path) -> str:
    failures = [record for record in records if not record["resolved"]]
    lines = [
        "# Public Dev Failure Analysis Index",
        "",
        "Use this index to select at least four failures. Fill in the first consequential",
        "failure, evidence, category, and prevention/recovery mechanism in your report.",
        "",
    ]
    if not failures:
        lines.extend(
            [
                "All tasks resolved in this run. Use a weaker ablation or an earlier run for",
                "the required failure analysis.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "| Task | Termination | Visible | F2P | P2P | Calls | Tokens | Artifacts |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for record in failures:
        task_output = Path(record["output_dir"])
        relative = Path(task_output).resolve().relative_to(output_root.resolve()).as_posix()
        artifacts = (
            f"[trajectory]({relative}/trajectory.jsonl), "
            f"[patch]({relative}/patch.diff), "
            f"[verification]({relative}/verification.json)"
        )
        lines.append(
            "| {task} | {termination} | {visible} | {f2p} | {p2p} | {calls} | "
            "{tokens} | {artifacts} |".format(
                task=record["task_id"],
                termination=record["termination_reason"],
                visible=_display_bool(record["visible_tests_passed"]),
                f2p=_display_bool(record["f2p_passed"]),
                p2p=_display_bool(record["p2p_passed"]),
                calls=record["model_calls"],
                tokens=record["total_tokens"],
                artifacts=artifacts,
            )
        )
    lines.extend(
        [
            "",
            "## Analysis Worksheet",
            "",
            "| Task | First consequential failure | Category | Evidence | Prevention/recovery |",
            "|---|---|---|---|---|",
        ]
    )
    for record in failures:
        lines.append(f"| `{record['task_id']}` |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def write_paired_reports(
    conditions: dict[str, list[dict[str, Any]]],
    output_root: Path,
    *,
    budget_variable: bool = False,
) -> dict[str, Any]:
    if len(conditions) < 2:
        raise ValueError("paired reporting requires at least two conditions")
    labels = list(conditions)
    expected_order = [record["task_id"] for record in conditions[labels[0]]]
    expected_seeds = [record["seed"] for record in conditions[labels[0]]]
    reference = conditions[labels[0]]
    if not reference or len(expected_order) != len(set(expected_order)):
        raise ValueError("paired reporting requires distinct nonempty tasks")
    for label in labels[1:]:
        order = [record["task_id"] for record in conditions[label]]
        seeds = [record["seed"] for record in conditions[label]]
        if order != expected_order:
            raise ValueError(f"condition {label!r} used a different task order")
        if seeds != expected_seeds:
            raise ValueError(f"condition {label!r} used different seeds")
    fixed = comparison_identity(reference[0].get("run", {}))
    caps = reference[0].get("budget")
    for label, records in conditions.items():
        for record in records:
            if comparison_identity(record.get("run", {})) != fixed:
                raise ValueError(f"condition {label!r} used different model/provider/settings/runtime")
            if not budget_variable and record.get("budget") != caps:
                raise ValueError("paired family budgets differ; declare --budget-variable")

    condition_summaries: dict[str, Any] = {}
    for label, records in conditions.items():
        successes = sum(bool(record["resolved"]) for record in records)
        interval = wilson_interval(successes, len(records)) if records else (0.0, 0.0)
        condition_summaries[label] = {
            "resolved": successes,
            "tasks": len(records),
            "resolve_rate": successes / len(records) if records else 0.0,
            "resolve_rate_wilson_95": {"low": interval[0], "high": interval[1]},
            "mean_model_calls": _numeric_summary(records, "model_calls")["mean"],
            "mean_total_tokens": _numeric_summary(records, "total_tokens")["mean"],
            "mean_active_seconds": _numeric_summary(records, "active_seconds")["mean"],
        }

    baseline = labels[0]
    comparisons: dict[str, Any] = {}
    baseline_records = conditions[baseline]
    for label in labels[1:]:
        candidate_records = conditions[label]
        wins = losses = ties = 0
        for base_record, candidate_record in zip(baseline_records, candidate_records):
            base_value = bool(base_record["resolved"])
            candidate_value = bool(candidate_record["resolved"])
            if candidate_value and not base_value:
                wins += 1
            elif base_value and not candidate_value:
                losses += 1
            else:
                ties += 1
        comparisons[label] = {
            "baseline": baseline,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "resolve_rate_delta": (
                condition_summaries[label]["resolve_rate"]
                - condition_summaries[baseline]["resolve_rate"]
            ),
        }

    summary = {
        "task_order": expected_order,
        "seed": expected_seeds[0] if expected_seeds else None,
        "budget_variable": budget_variable,
        "comparison_identity": fixed,
        "conditions": condition_summaries,
        "comparisons_to_first_condition": comparisons,
        "interpretation_note": (
            "Results are experiment evidence, not a fixed-model performance grade. "
            "Twelve paired tasks cannot establish small performance differences; "
            "treat one-task changes as hypotheses, not proof."
        ),
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "paired_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_paired_csv(conditions, output_root / "paired_tasks.csv")
    (output_root / "paired_summary.md").write_text(
        _render_paired_markdown(summary),
        encoding="utf-8",
    )
    return summary


def _write_paired_csv(
    conditions: dict[str, list[dict[str, Any]]],
    path: Path,
) -> None:
    labels = list(conditions)
    fields = ["task_id"]
    for label in labels:
        fields.extend(
            [
                f"{label}.resolved",
                f"{label}.model_calls",
                f"{label}.total_tokens",
                f"{label}.active_seconds",
            ]
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, task_id in enumerate(
            record["task_id"] for record in conditions[labels[0]]
        ):
            row: dict[str, Any] = {"task_id": task_id}
            for label in labels:
                record = conditions[label][index]
                row[f"{label}.resolved"] = record["resolved"]
                row[f"{label}.model_calls"] = record["model_calls"]
                row[f"{label}.total_tokens"] = record["total_tokens"]
                row[f"{label}.active_seconds"] = record["active_seconds"]
            writer.writerow(row)


def _render_paired_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Paired Experiment Summary",
        "",
        "| Condition | Resolved | Rate | Wilson 95% | Mean calls | Mean tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, item in summary["conditions"].items():
        interval = item["resolve_rate_wilson_95"]
        lines.append(
            f"| `{label}` | {item['resolved']}/{item['tasks']} | "
            f"{item['resolve_rate']:.3f} | "
            f"[{interval['low']:.3f}, {interval['high']:.3f}] | "
            f"{item['mean_model_calls']:.2f} | {item['mean_total_tokens']:.1f} |"
        )
    lines.extend(
        [
            "",
            "| Comparison | Wins | Losses | Ties | Resolve-rate delta |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for label, item in summary["comparisons_to_first_condition"].items():
        lines.append(
            f"| `{label}` vs `{item['baseline']}` | {item['wins']} | "
            f"{item['losses']} | {item['ties']} | "
            f"{item['resolve_rate_delta']:+.3f} |"
        )
    lines.extend(["", summary["interpretation_note"], ""])
    return "\n".join(lines)


def _reported_number(value: Any) -> int | float | None:
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _numeric_summary(records: list[dict[str, Any]], field: str) -> dict[str, float]:
    values = [float(value) for record in records
              if (value := _reported_number(record.get(field))) is not None]
    counts = {"observed": len(values), "missing": len(records) - len(values)}
    if not values:
        return {"mean": 0.0, "median": 0.0, "max": 0.0, **counts}
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "max": max(values),
        **counts,
    }


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default
    return payload if isinstance(payload, dict) else default


def _display_bool(value: Any) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "unknown"
