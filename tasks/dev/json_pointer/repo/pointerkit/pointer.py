def resolve_pointer(document, pointer):
    if pointer == "":
        return document
    current = document
    for token in pointer.lstrip("/").split("/"):
        current = current[token]
    return current
