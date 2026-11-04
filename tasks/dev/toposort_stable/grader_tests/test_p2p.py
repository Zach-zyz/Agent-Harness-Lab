from graphkit import topological_sort


def test_chain_independent_nodes_and_empty_graph() -> None:
    assert topological_sort({"c": ["b"], "b": ["a"], "z": []}) == [
        "a",
        "b",
        "c",
        "z",
    ]
    assert topological_sort({}) == []
