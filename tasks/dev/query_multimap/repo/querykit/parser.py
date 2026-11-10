from urllib.parse import parse_qsl


def parse_query(query):
    return {key: [value] for key, value in parse_qsl(query)}
