"""Deterministic, offline check of the localized-context mechanism.

Run from the release root:  python -m harness.check_localize
No API key, network, or model is used.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .agent import RepairAgent
from .budget import Budget
from .localize import build_context_pack, rank_source_files, visible_test_paths
from .model_client import ModelResponse

ISSUE = "slugify keeps uppercase letters. Lowercase the result of slugify."
VISIBLE = "python -m pytest -q tests/test_visible.py"


def _make_repo(root: Path) -> None:
    (root / "textkit").mkdir()
    (root / "tests").mkdir()
    (root / "textkit" / "__init__.py").write_text(
        "from .slug import slugify\n", encoding="utf-8")
    (root / "textkit" / "slug.py").write_text(
        "def slugify(text):\n    return text.replace(' ', '-')\n", encoding="utf-8")
    (root / "textkit" / "unrelated.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_visible.py").write_text(
        "from textkit import slugify\n\n\ndef test_dash():\n"
        "    assert slugify('a b') == 'a-b'\n", encoding="utf-8")


class _RecordingClient:
    """Returns finish once and records the messages it was sent."""

    def __init__(self) -> None:
        self.first_messages: list[dict[str, str]] | None = None

    def complete(self, messages, seed):
        del seed
        if self.first_messages is None:
            self.first_messages = [dict(message) for message in messages]
        return ModelResponse(json.dumps({"action": "finish", "args": {"summary": "x"}}), 1, 1)


def _first_prompt(worktree: Path, enabled: bool) -> tuple[str, list[str]]:
    client = _RecordingClient()
    agent = RepairAgent(client, Budget(max_model_calls=1), 2026, localized_context=enabled)
    state = agent.run(ISSUE, worktree, required_test_command=VISIBLE)
    text = "\n".join(message["content"] for message in client.first_messages or [])
    return text, [event.kind for event in state.events]


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _make_repo(root)
        tests = visible_test_paths(root, VISIBLE)
        assert [path.name for path in tests] == ["test_visible.py"], tests

        ranked = rank_source_files(root, ISSUE, tests)
        assert ranked[0]["path"] == "textkit/slug.py", ranked
        assert ranked[0]["score"] > ranked[1]["score"] > 0, ranked
        assert ranked[-1]["path"] == "textkit/unrelated.py" and ranked[-1]["score"] == 0

        prompt, details = build_context_pack(root, ISSUE, tests)
        assert details["included"] == ["textkit/slug.py", "textkit/__init__.py"], details
        assert "2:     return text.replace(' ', '-')" in prompt
        assert "unrelated.py" not in details["included"]
        assert build_context_pack(root, ISSUE, tests)[0] == prompt  # deterministic

        on_text, on_kinds = _first_prompt(root, True)
        off_text, off_kinds = _first_prompt(root, False)
        assert "Localized context" in on_text and "localization" in on_kinds
        assert "Localized context" not in off_text and "localization" not in off_kinds
        assert (root / "textkit" / "slug.py").read_text(encoding="utf-8").startswith(
            "def slugify")  # the mechanism never writes files

    print("check_localize: all checks passed")


if __name__ == "__main__":
    main()
