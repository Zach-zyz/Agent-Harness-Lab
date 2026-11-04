"""Student-owned agent loop."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .budget import Budget, BudgetExceeded
from .localize import extract_failure_summary
from .model_client import ModelClient
from .state import AgentState
from .tools import ToolError, ToolRunner


SYSTEM_PROMPT = """You are a code-repair agent. Respond with exactly one JSON object:
{"action": "list_files|read_file|search|write_file|run_tests|finish",
 "args": {...},
 "thought": "brief rationale"}
Action arguments:
- list_files: {"path": "."}
- read_file: {"path": "relative/path.py", "start_line": 1, "end_line": 240}
- search: {"query": "text", "path": "."}
- write_file: {"path": "relative/path.py", "content": "complete file content"}
- run_tests: {"command": "python -m pytest -q"}
- finish: {"summary": "what changed and what passed"}
Use only relative paths. Never edit tests, evaluator/configuration files, dependency files, or
runtime-injection files. The issue description is the acceptance contract and visible tests are
incomplete. Satisfy every explicit clause, preserve existing behavior, and audit input types, edge
cases, and regressions before writing and before finishing. Inspect before editing, run tests after
editing, and finish only after reserving enough budget to emit a patch."""


class RepairAgent:
    def __init__(
        self,
        client: ModelClient,
        budget: Budget,
        seed: int,
        *,
        bootstrap_context: bool = False,
        auto_test_after_write: bool = False,
    ) -> None:
        self.client = client
        self.budget = budget
        self.seed = seed
        self.bootstrap_context = bootstrap_context
        self.auto_test_after_write = auto_test_after_write

    def run(
        self,
        issue: str,
        worktree: str | Path,
        required_test_command: str = "python -m pytest -q",
    ) -> AgentState:
        """Run the model-tool loop until finish, budget exhaustion, or a fatal error."""
        state = AgentState(issue=issue)
        self.state = state
        tools = ToolRunner(worktree, self.budget)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Issue:\n{issue}\n\n"
                    f"Designated full visible-test command:\n{required_test_command}\n\n"
                    "Inspect the repository, make a source-only fix, run visible tests, "
                    "and finish with a concise summary."
                ),
            },
        ]
        state.record("state", status=state.status, step=state.step)

        try:
            if self.bootstrap_context:
                bootstrap_observations = []
                for action_name, arguments in (
                    ("list_files", {"path": "."}),
                    ("run_tests", {"command": required_test_command}),
                ):
                    observation = tools.execute(action_name, arguments)
                    state.record(
                        "tool",
                        step=0,
                        name=action_name,
                        arguments=arguments,
                        observation=observation,
                        automatic=True,
                    )
                    bootstrap_observations.append(
                        f"Automatic {action_name} observation:\n{observation}"
                    )
                    if action_name == "run_tests":
                        state.summary = extract_failure_summary(observation)
                        state.visible_tests_passed = observation.startswith("exit_code: 0")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "\n\n".join(bootstrap_observations)
                            + "\n\nUse this preflight context to localize the fix. "
                            "Do not repeat these actions unless the repository changes."
                        ),
                    }
                )

            while self.budget.model_calls < self.budget.max_model_calls:
                self.budget.check()
                state.step += 1
                state.record("model_request", step=state.step, messages=len(messages))
                self.budget.start_model_call()
                state.record("budget", **self.budget.metrics())
                response = self.client.complete(messages, self.seed)
                state.record(
                    "model",
                    step=state.step,
                    content=response.content,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
                # Keep the returned response even when charging it exhausts the
                # budget; it is evidence for the failed trial, not an action to run.
                if not response.usage_charged:
                    self.budget.charge_model_tokens(response.input_tokens, response.output_tokens)
                state.record("budget", **self.budget.metrics())
                messages.append({"role": "assistant", "content": response.content})

                try:
                    action = _parse_action(response.content)
                except (ValueError, json.JSONDecodeError) as error:
                    observation = f"Invalid JSON action: {error}"
                    state.last_observation = observation
                    state.record("error", step=state.step, message=observation)
                    messages.append(
                        {
                            "role": "user",
                            "content": observation + ". Respond with one valid action object.",
                        }
                    )
                    continue

                action_name = action["action"]
                arguments = action["args"]
                if action_name == "finish":
                    if state.visible_tests_passed is not True:
                        observation = (
                            "Finish rejected: after the most recent write, run a pytest command "
                            f"covering the designated visible scope: {required_test_command!r}."
                        )
                        state.last_observation = observation
                        state.record("error", step=state.step, message=observation)
                        messages.append({"role": "user", "content": observation})
                        continue
                    summary = arguments.get("summary", action.get("thought", ""))
                    state.summary = summary if isinstance(summary, str) else str(summary)
                    state.status = "finished"
                    state.record("finish", step=state.step, summary=state.summary)
                    state.record("state", status=state.status, step=state.step)
                    break

                try:
                    observation = tools.execute(action_name, arguments)
                    tool_succeeded = True
                except ToolError as error:
                    observation = f"Tool error: {error}"
                    tool_succeeded = False
                    state.record(
                        "error",
                        step=state.step,
                        action=action_name,
                        message=str(error),
                    )
                state.last_observation = observation
                state.record(
                    "tool",
                    step=state.step,
                    name=action_name,
                    arguments=arguments,
                    observation=observation,
                )
                if action_name == "write_file" and tool_succeeded:
                    state.visible_tests_passed = None
                    if self.auto_test_after_write:
                        automatic_arguments = {"command": required_test_command}
                        automatic_observation = tools.execute(
                            "run_tests",
                            automatic_arguments,
                        )
                        state.record(
                            "tool",
                            step=state.step,
                            name="run_tests",
                            arguments=automatic_arguments,
                            observation=automatic_observation,
                            automatic=True,
                        )
                        state.summary = extract_failure_summary(automatic_observation)
                        state.visible_tests_passed = automatic_observation.startswith(
                            "exit_code: 0"
                        )
                        observation += (
                            "\n\nAutomatic designated visible-test result after write:\n"
                            + automatic_observation
                        )
                if action_name == "run_tests":
                    state.summary = extract_failure_summary(observation)
                    command = arguments.get("command", "python -m pytest -q")
                    state.visible_tests_passed = (
                        tool_succeeded
                        and observation.startswith("exit_code: 0")
                        and _pytest_command_covers(command, required_test_command)
                    )
                state.record("budget", **self.budget.metrics())
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Tool observation for {action_name}:\n{observation}\n\n"
                            f"Original issue contract:\n{issue}\n\n"
                            "Before the next write or finish action, audit every explicit issue "
                            "clause and likely regressions not covered by visible tests.\n"
                            f"Remaining model calls: "
                            f"{self.budget.max_model_calls - self.budget.model_calls}."
                        ),
                    }
                )
            else:
                state.status = "budget_exhausted"
                state.record("state", status=state.status, step=state.step)
        except KeyboardInterrupt:
            state.status = "interrupted"
            state.last_observation = "Interrupted; preserve this trial and use a new output directory."
            state.record("budget", **self.budget.metrics())
            state.record("error", step=state.step, message=state.last_observation)
            state.record("state", status=state.status, step=state.step)
        except BudgetExceeded as error:
            state.status = "budget_exhausted"
            state.last_observation = str(error)
            state.record("budget", **self.budget.metrics())
            state.record("error", step=state.step, message=str(error))
            state.record("state", status=state.status, step=state.step)
        except Exception as error:
            state.status = "error"
            state.last_observation = f"{type(error).__name__}: {error}"
            state.record("budget", **self.budget.metrics())
            state.record("error", step=state.step, message=state.last_observation)
            state.record("state", status=state.status, step=state.step)

        return state


def _parse_action(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        object_start = content.find("{")
        object_end = content.rfind("}")
        if object_start < 0 or object_end <= object_start:
            raise
        payload = json.loads(content[object_start : object_end + 1])

    if not isinstance(payload, dict):
        raise ValueError("response must be a JSON object")
    action = payload.get("action")
    allowed_actions = {
        "list_files",
        "read_file",
        "search",
        "write_file",
        "run_tests",
        "finish",
    }
    if action not in allowed_actions:
        raise ValueError(f"unknown action: {action!r}")
    arguments = payload.get("args", {})
    if not isinstance(arguments, dict):
        raise ValueError("args must be a JSON object")
    if "path" not in arguments and isinstance(arguments.get("file_path"), str):
        arguments["path"] = arguments.pop("file_path")
    thought = payload.get("thought", "")
    if not isinstance(thought, str):
        raise ValueError("thought must be a string")
    return {"action": action, "args": arguments, "thought": thought}


def _pytest_arguments(command: str) -> list[str]:
    if not isinstance(command, str):
        return []
    arguments = shlex.split(command)
    if not arguments:
        return []
    executable = Path(arguments[0]).name
    if executable in {"python", "python3"} and arguments[1:3] == ["-m", "pytest"]:
        return arguments[3:]
    if executable == "pytest":
        return arguments[1:]
    return []


def _pytest_command_covers(actual: str, required: str) -> bool:
    """Return whether the exact staff-designated visible command was run.

    Diagnostic subsets are allowed, but options that alter collection must never certify the
    episode as locally resolved.
    """
    return _pytest_arguments(actual) == _pytest_arguments(required)
