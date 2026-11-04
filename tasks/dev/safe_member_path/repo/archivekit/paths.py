from pathlib import Path


def safe_member_path(root, member):
    if member.startswith("/"):
        raise ValueError("absolute member path")
    return Path(root) / member
