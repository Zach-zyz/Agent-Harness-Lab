import pytest

from harness.budget import Budget, BudgetExceeded


def test_budget_counts_model_tokens_and_tools() -> None:
    budget = Budget(max_model_calls=2, max_tokens=100)
    budget.charge_model_call(20, 10)
    budget.charge_tool("read_file", 0.01)
    assert budget.model_calls == 1
    assert budget.total_tokens == 30
    assert budget.tool_calls == {"read_file": 1}


def test_budget_raises_after_hard_cap() -> None:
    budget = Budget(max_model_calls=1)
    budget.charge_model_call(1, 1)
    with pytest.raises(BudgetExceeded):
        budget.charge_model_call(1, 1)


def test_thirty_minute_budget_boundary(monkeypatch):
    import harness.budget as module
    budget = Budget(started_at=0)
    assert budget.max_active_seconds == 1800
    monkeypatch.setattr(module.time, "monotonic", lambda: 1800)
    budget.check()
    monkeypatch.setattr(module.time, "monotonic", lambda: 1800.001)
    with pytest.raises(BudgetExceeded, match="active-time budget exceeded"):
        budget.check()
