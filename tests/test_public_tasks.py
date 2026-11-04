import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_tasks_are_complete_and_solution_free() -> None:
    tasks = sorted(path for path in (ROOT / "tasks" / "dev").iterdir() if path.is_dir())
    assert len(tasks) == 12
    for task in tasks:
        metadata = json.loads((task / "task.json").read_text())
        assert metadata["id"].startswith("dev_")
        assert (task / "repo" / "tests" / "test_visible.py").is_file()
        assert (task / "grader_tests" / "test_f2p.py").is_file()
        assert (task / "grader_tests" / "test_p2p.py").is_file()
        assert not (task / "replay.json").exists()
        assert not any(path.name == "grader_tests" for path in (task / "repo").rglob("*"))
