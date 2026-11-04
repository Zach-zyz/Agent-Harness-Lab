import pytest

from graphkit import topological_sort


def test_stable_ready_order_and_duplicate_dependencies() -> None:
    graph = {
        "package": ["build", "build"],
        "lint": [],
        "build": ["compile"],
    }
    assert topological_sort(graph) == ["lint", "compile", "build", "package"]


def test_self_cycle_is_rejected() -> None:
    with pytest.raises(ValueError):
        topological_sort({"a": ["a"]})
