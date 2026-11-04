import pytest

from archivekit import safe_member_path


@pytest.mark.parametrize(
    "member",
    ["../secret.txt", "pkg/../../secret.txt", r"..\secret.txt", r"C:\temp\x.txt"],
)
def test_traversal_and_drive_paths_are_rejected(tmp_path, member) -> None:
    with pytest.raises(ValueError):
        safe_member_path(tmp_path, member)


def test_nested_member_resolves_beneath_root(tmp_path) -> None:
    assert safe_member_path(tmp_path, "pkg/data.txt") == (tmp_path / "pkg/data.txt").resolve()
