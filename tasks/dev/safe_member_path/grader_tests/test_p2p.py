from archivekit import safe_member_path


def test_regular_relative_members_still_resolve(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert safe_member_path(root, "file.txt") == (root / "file.txt").resolve()
    assert safe_member_path(root, "a/b/c.py") == (root / "a/b/c.py").resolve()
