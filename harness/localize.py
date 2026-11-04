"""Optional localization helpers."""

from __future__ import annotations


def extract_failure_summary(test_output: str, max_lines: int = 40) -> str:
    """Return a compact failure-oriented summary of test output."""
    lines = test_output.splitlines()
    selected = [
        line
        for line in lines
        if (
            line.startswith(("FAILED ", "ERROR ", "E ", "exit_code:"))
            or "AssertionError" in line
            or "Traceback" in line
        )
    ]
    if not selected:
        selected = lines[-max_lines:]
    return "\n".join(selected[:max_lines])
