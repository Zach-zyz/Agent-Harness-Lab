def dedupe_keep_last(items, key=lambda item: item):
    return list({key(item): item for item in items}.values())
