"""Run both toy tasks through the required CLI and verifier."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .report import json_safe
from .validate_patch import validate_patch


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the two offline replay tutorials and fresh verifier.")
    parser.add_argument("--output_root", type=Path, default=ROOT / ".pilot-output")
    args = parser.parse_args()
    results = {}
    output_root = args.output_root.resolve()
    try:
        output_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("output directory already exists; preserve the previous trial and choose a new --output_root")
    for task_dir in sorted((ROOT / "tasks" / "toy").iterdir()):
        if not task_dir.is_dir():
            continue
        output_dir = output_root / task_dir.name
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_agent.py"),
                "--task_dir",
                str(task_dir),
                "--output_dir",
                str(output_dir),
                "--seed",
                "2026",
                "--backend",
                "replay",
            ],
            cwd=ROOT,
            check=True,
        )
        results[task_dir.name] = validate_patch(task_dir, output_dir / "patch.diff")
    print(json.dumps(json_safe(results), indent=2, sort_keys=True))
    (output_root / "verification.json").write_text(
        json.dumps(json_safe(results), indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    if not all(result["resolved"] for result in results.values()):
        raise SystemExit("At least one toy task was not resolved")


if __name__ == "__main__":
    main()
