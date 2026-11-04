"""Structured state and trajectory records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TrajectoryEvent:
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentState:
    issue: str
    step: int = 0
    status: str = "running"
    summary: str = ""
    last_observation: str = ""
    visible_tests_passed: bool | None = None
    events: list[TrajectoryEvent] = field(default_factory=list)

    def record(self, kind: str, **payload: Any) -> None:
        self.events.append(TrajectoryEvent(kind=kind, payload=payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "step": self.step,
            "status": self.status,
            "summary": self.summary,
            "last_observation": self.last_observation,
            "visible_tests_passed": self.visible_tests_passed,
        }
