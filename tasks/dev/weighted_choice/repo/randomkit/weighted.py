def choose_weighted(items, weights, draw):
    cumulative = 0
    for item, weight in zip(items, weights):
        cumulative += weight
        if draw <= cumulative:
            return item
    raise ValueError("draw outside total weight")
