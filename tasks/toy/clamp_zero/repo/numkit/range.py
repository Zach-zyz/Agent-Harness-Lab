def clamp(value, lower=None, upper=None):
    if lower and upper and lower > upper:
        raise ValueError("lower bound exceeds upper bound")
    if lower and value < lower:
        value = lower
    if upper and value > upper:
        value = upper
    return value
