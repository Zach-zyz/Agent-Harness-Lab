def merge_intervals(intervals):
    ordered = sorted(intervals)
    merged = []
    for start, end in ordered:
        if not merged or merged[-1][1] < start:
            merged.append([start, end])
        elif start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
    return merged
