import pytest

from archivekit import safe_member_path


@pytest.mark.parametrize("member", ["", ".", "a/./b", "/etc/passwd", "bad\x00name"])
def test_malformed_names_are_rejected(tmp_path, member) -> None:
    with pytest.raises(ValueError):
        safe_member_path(tmp_path, member)


def test_existing_symlink_escape_is_rejected(tmp_path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        safe_member_path(root, "link/secret.txt")
