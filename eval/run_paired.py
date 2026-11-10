"""Run matched public-dev conditions and produce paired comparisons."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .report import write_paired_reports
from .run_dev import ROOT, run_batch


LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def parse_condition(value: str) -> tuple[str, Path]:
    label, separator, path_text = value.partition("=")
    if not separator or not LABEL_PATTERN.fullmatch(label) or not path_text:
        raise ValueError("conditions must use NAME=CONFIG_JSON")
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"condition config does not exist: {path}")
    return label, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--condition",
        action="append",
        required=True,
        help="Matched condition as NAME=CONFIG_JSON; repeat at least twice.",
    )
    parser.add_argument("--task_root", default=str(ROOT / "tasks" / "dev"))
    parser.add_argument("--output_root", required=True)
    parser.add_argument("--backend", choices=("endpoint", "replay"), default="endpoint")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--task", action="append", dest="tasks")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--budget-variable", action="store_true",
                        help="Declare episode budget as a variable within the paired family.")
    args = parser.parse_args()

    try:
        parsed = [parse_condition(value) for value in args.condition]
    except ValueError as error:
        raise SystemExit(str(error)) from error
    labels = [label for label, _ in parsed]
    if len(labels) < 2:
        raise SystemExit("provide at least two --condition values")
    if len(labels) != len(set(labels)):
        raise SystemExit("condition labels must be unique")
    defaults = {
        "temperature": 0.2, "top_p": 1.0, "max_output_tokens": 4096,
        "max_model_calls": 12, "max_total_tokens": 120000, "max_active_seconds": 1800,
    }
    fixed = None
    for label, config in parsed:
        payload = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit(f"condition {label!r} config must be an object")
        settings = {key: payload.get(key, default) for key, default in defaults.items()
                    if not args.budget_variable or key in {"temperature", "top_p", "max_output_tokens"}}
        if fixed is not None and settings != fixed:
            raise SystemExit("conditions must fix sampling settings and family caps; declare --budget-variable for caps")
        fixed = settings

    output_root = Path(args.output_root).resolve()
    task_root = Path(args.task_root).resolve()
    conditions = {}
    for label, config in parsed:
        condition_root = output_root / "conditions" / label
        summary = run_batch(
            task_root=task_root,
            output_root=condition_root,
            backend=args.backend,
            seed=args.seed,
            config=config,
            selected=args.tasks,
            resume=args.resume,
            split=f"public-dev:{label}",
        )
        conditions[label] = summary["records"]
    try:
        paired = write_paired_reports(conditions, output_root, budget_variable=args.budget_variable)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(paired, indent=2, sort_keys=True))
    if any(record["outcome"] == "error" for records in conditions.values() for record in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
