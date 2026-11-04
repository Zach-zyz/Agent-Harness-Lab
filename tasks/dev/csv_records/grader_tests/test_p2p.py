from csvkit import read_records


def test_simple_records_and_empty_input() -> None:
    assert read_records("name,score\nAda,10\nGrace,9\n") == [
        {"name": "Ada", "score": "10"},
        {"name": "Grace", "score": "9"},
    ]
    assert read_records("") == []
