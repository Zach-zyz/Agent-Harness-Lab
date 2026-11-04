import json
from pathlib import Path

import pytest

from harness.agent import (
    RepairAgent,
    _parse_action,
    _pytest_arguments,
    _pytest_command_covers,
)
from harness.budget import Budget
from harness.model_client import ModelResponse


def test_parse_action_accepts_json_inside_fenced_response() -> None:
    action = _parse_action(
        'Here is the action:\n```json\n{"action":"list_files","args":{"path":"."}}\n```'
    )
    assert action["action"] == "list_files"


def test_parse_action_normalizes_observed_file_path_alias() -> None:
    action = _parse_action(
        json.dumps({"action": "read_file", "args": {"file_path": "pkg/a.py"}})
    )
    assert action["args"] == {"path": "pkg/a.py"}


def test_parse_action_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        _parse_action(json.dumps({"action": "shell", "args": {"command": "id"}}))


def test_pytest_argument_normalization_distinguishes_subsets() -> None:
    assert _pytest_arguments("pytest -q") == _pytest_arguments("python -m pytest -q")
    assert _pytest_arguments("pytest -q tests/test_visible.py") != _pytest_arguments(
        "pytest -q"
    )
    assert _pytest_command_covers(
        "python -m pytest -q tests/test_visible.py",
        "python -m pytest -q tests/test_visible.py",
    )
    assert not _pytest_command_covers(
        "python -m pytest -q tests/test_visible.py --collect-only",
        "python -m pytest -q tests/test_visible.py",
    )
    assert not _pytest_command_covers(
        "python -m pytest -q tests/test_visible.py --ignore=tests/test_visible.py",
        "python -m pytest -q tests/test_visible.py",
    )
    assert not _pytest_command_covers(
        "python -m pytest -q tests/test_visible.py::test_one",
        "python -m pytest -q",
    )


class _SequenceClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses

    def complete(self, messages: list[dict[str, str]], seed: int) -> ModelResponse:
        del messages, seed
        payload = self.responses.pop(0)
        return ModelResponse(json.dumps(payload), 10, 10)


def test_first_response_over_budget_is_logged_without_executing_its_action(tmp_path):
    budget = Budget(max_tokens=1)
    client = _SequenceClient([
        {"action": "write_file", "args": {"path": "must-not-exist.py", "content": "VALUE = 1"}}
    ])
    state = RepairAgent(client, budget, 2026).run("Repair the repository.", tmp_path)
    assert state.status == "budget_exhausted"
    assert not (tmp_path / "must-not-exist.py").exists()
    responses = [event.payload for event in state.events if event.kind == "model"]
    assert len(responses) == 1
    assert responses[0]["input_tokens"] == 10
    assert responses[0]["output_tokens"] == 10
    final_budget = [event.payload for event in state.events if event.kind == "budget"][-1]
    assert final_budget["total_tokens"] == 20
    assert final_budget["model_calls"] == 1


def test_write_after_passing_tests_invalidates_visible_status(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_visible.py").write_text(
        "from pkg import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    client = _SequenceClient(
        [
            {
                "action": "run_tests",
                "args": {"command": "python -m pytest -q"},
                "thought": "test",
            },
            {
                "action": "write_file",
                "args": {"path": "pkg.py", "content": "VALUE = 2\n"},
                "thought": "edit",
            },
            {"action": "finish", "args": {"summary": "done"}, "thought": "finish"},
        ]
    )
    state = RepairAgent(client, Budget(), 2026).run("Keep tests passing.", tmp_path)
    assert state.visible_tests_passed is None


def test_passing_subset_is_not_full_visible_status(tmp_path: Path) -> None:
    (tmp_path / "test_visible.py").write_text(
        "def test_one(): assert True\n\ndef test_two(): assert True\n",
        encoding="utf-8",
    )
    client = _SequenceClient(
        [
            {
                "action": "run_tests",
                "args": {
                    "command": "python -m pytest -q test_visible.py::test_one"
                },
                "thought": "subset",
            },
            {"action": "finish", "args": {"summary": "done"}, "thought": "finish"},
        ]
    )
    state = RepairAgent(client, Budget(), 2026).run(
        "Run all tests.",
        tmp_path,
        required_test_command="python -m pytest -q",
    )
    assert state.visible_tests_passed is False
    assert state.status == "error"


def test_collect_only_cannot_certify_visible_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_visible.py").write_text(
        "def test_failure(): assert False\n",
        encoding="utf-8",
    )
    client = _SequenceClient(
        [
            {
                "action": "run_tests",
                "args": {
                    "command": (
                        "python -m pytest -q tests/test_visible.py --collect-only"
                    )
                },
                "thought": "collect",
            },
            {"action": "finish", "args": {"summary": "done"}, "thought": "finish"},
        ]
    )
    state = RepairAgent(client, Budget(), 2026).run(
        "Run the designated tests.",
        tmp_path,
        required_test_command="python -m pytest -q tests/test_visible.py",
    )
    assert state.visible_tests_passed is False
    assert state.status != "finished"


class _FailingClient:
    def complete(self, messages: list[dict[str, str]], seed: int) -> ModelResponse:
        del messages, seed
        raise TimeoutError("model request timed out")


def test_failed_model_request_consumes_call_budget(tmp_path: Path) -> None:
    budget = Budget()
    state = RepairAgent(_FailingClient(), budget, 2026).run("Inspect.", tmp_path)
    assert budget.model_calls == 1
    assert budget.total_tokens == 0
    assert state.status == "error"
    budget_events = [
        event.payload for event in state.events if event.kind == "budget"
    ]
    assert budget_events[-1]["model_calls"] == 1
    assert budget_events[-1]["total_tokens"] == 0


def test_strong_profile_bootstraps_and_auto_tests_after_write(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text("VALUE = 0\n", encoding="utf-8")
    (tmp_path / "test_visible.py").write_text(
        "from pkg import VALUE\n\ndef test_value(): assert VALUE == 1\n",
        encoding="utf-8",
    )
    client = _SequenceClient(
        [
            {
                "action": "write_file",
                "args": {"path": "pkg.py", "content": "VALUE = 1\n"},
                "thought": "fix",
            },
            {"action": "finish", "args": {"summary": "done"}, "thought": "finish"},
        ]
    )
    state = RepairAgent(
        client,
        Budget(),
        2026,
        bootstrap_context=True,
        auto_test_after_write=True,
    ).run("Set VALUE to one.", tmp_path)
    assert state.status == "finished"
    assert state.visible_tests_passed is True
    automatic = [
        event
        for event in state.events
        if event.kind == "tool" and event.payload.get("automatic") is True
    ]
    assert [event.payload["name"] for event in automatic] == [
        "list_files",
        "run_tests",
        "run_tests",
    ]
