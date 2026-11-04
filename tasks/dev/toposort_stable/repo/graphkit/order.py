def topological_sort(graph):
    result = []
    visited = set()

    def visit(node):
        if node in visited:
            return
        visited.add(node)
        for dependency in graph.get(node, ()):
            visit(dependency)
        result.append(node)

    for node in graph:
        visit(node)
    return result
