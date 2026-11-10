import json
import subprocess
import sys
from pathlib import Path

import pytest

from eval.validate_patch import validate_patch


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("task_name", ["parse_bool", "clamp_zero"])
def test_replay_agent_resolves_toy_task(task_name: str, tmp_path: Path) -> None:
    task_dir = ROOT / "tasks" / "toy" / task_name
    output_dir = tmp_path / task_name
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
    for name in ["patch.diff", "trajectory.jsonl", "metrics.json", "final_state.json"]:
        assert (output_dir / name).is_file()
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "api_attempts.jsonl",
        "final_state.json",
        "metrics.json",
        "patch.diff",
        "provider_metrics.json",
        "trajectory.jsonl",
    ]
    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["model_calls"] <= 12
    assert metrics["resolved"] is True
    events = [
        json.loads(line)
        for line in (output_dir / "trajectory.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert {event["kind"] for event in events} >= {"model", "tool", "finish"}
    assert validate_patch(task_dir, output_dir / "patch.diff")["resolved"]
