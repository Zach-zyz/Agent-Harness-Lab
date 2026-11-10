"""Staff-owned budget accounting. Student submissions may not modify this file."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_model_calls: int = 12
    max_tokens: int = 120_000
    max_active_seconds: float = 1800.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    test_invocations: int = 0
    test_seconds: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    def start_model_call(self) -> None:
        self.model_calls += 1
        self.check()

    def charge_model_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.check()

    def charge_model_call(self, input_tokens: int, output_tokens: int) -> None:
        self.start_model_call()
        self.charge_model_tokens(input_tokens, output_tokens)

    def charge_tool(self, name: str, elapsed_seconds: float, is_test: bool = False) -> None:
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
        if is_test:
            self.test_invocations += 1
            self.test_seconds += elapsed_seconds
        self.check()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def active_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> None:
        if self.model_calls > self.max_model_calls:
            raise BudgetExceeded("model-call budget exceeded")
        if self.total_tokens > self.max_tokens:
            raise BudgetExceeded("token budget exceeded")
        if self.active_seconds > self.max_active_seconds:
            raise BudgetExceeded("active-time budget exceeded")

    def metrics(self) -> dict:
        data = asdict(self)
        data["total_tokens"] = self.total_tokens
        data["active_seconds"] = self.active_seconds
        data.pop("started_at", None)
        return data
