def read_records(text):
    lines = [line for line in text.splitlines() if line]
    if not lines:
        return []
    header = lines[0].split(",")
    return [dict(zip(header, line.split(","))) for line in lines[1:]]
