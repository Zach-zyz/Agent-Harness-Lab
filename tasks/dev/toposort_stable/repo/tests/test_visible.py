import pytest

from graphkit import topological_sort


def test_cycle_is_rejected() -> None:
    with pytest.raises(ValueError):
        topological_sort({"a": ["b"], "b": ["a"]})


def test_dependency_only_nodes_are_included() -> None:
    assert topological_sort({"build": ["compile"], "test": ["compile"]}) == [
        "compile",
        "build",
        "test",
    ]
