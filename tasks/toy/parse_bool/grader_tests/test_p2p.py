from boolkit import parse_bool


def test_true_forms_and_bool_inputs_still_work():
    for value in ["true", " TRUE ", "yes", "Yes", "1"]:
        assert parse_bool(value) is True
    assert parse_bool(True) is True
    assert parse_bool(False) is False
