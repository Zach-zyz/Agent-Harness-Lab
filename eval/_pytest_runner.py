"""Staff-owned pytest entry point, using an anonymous pipe for lifecycle events.

The pipe has no report pathname and bootstrap arguments are consumed before
source imports. It is NOT an authenticated channel against arbitrary Python in
this process: introspection can recover descriptors and runner objects. The
parent's source policy and independent original-repo inventory bound this local
integrity check. Hostile-code isolation still requires an OS security boundary.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
import _pytest.python
import _pytest.runner


class TestReports:
    def __init__(self, emit) -> None:
        self.emit = emit
        self.check_references = lambda: None

    def pytest_collectreport(self, report) -> None:
        if report.outcome != "passed":
            self.emit("collection_issue", nodeid=report.nodeid, outcome=report.outcome)

    def pytest_deselected(self, items) -> None:
        self.emit("deselected", nodeids=[item.nodeid for item in items])

    def pytest_collection_finish(self, session) -> None:
        self.check_references()
        self.emit("collection_complete", nodeids=[item.nodeid for item in session.items])

    def pytest_runtest_logstart(self, nodeid, location) -> None:
        self.check_references()

    def pytest_runtest_logreport(self, report) -> None:
        self.check_references()
        self.emit(
            "test_report",
            nodeid=report.nodeid,
            when=report.when,
            outcome=report.outcome,
            # Even a non-strict XPASS is not an unconditional pass.
            wasxfail=hasattr(report, "wasxfail"),
        )

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.check_references()
        self.emit("session_finish", exitstatus=int(exitstatus))


def _watch_runtime_references(reports, emit):
    """Catch direct monkeypatches of a deliberately small set of pytest hooks.

    This is not exhaustive runtime attestation. Python sharing the process can
    introspect or subvert the watcher too; the parent source policy is required.
    """
    pairs = [
        (pytest, "main"),
        (_pytest.python.PyCollector, "collect"),
        (_pytest.python.Function, "runtest"),
        (_pytest.python, "pytest_pyfunc_call"),
        (_pytest.runner, "call_and_report"),
        (_pytest.runner, "pytest_runtest_protocol"),
        (_pytest.runner, "pytest_runtest_call"),
        (_pytest.runner, "pytest_runtest_makereport"),
        (reports, "emit"),
        *[(TestReports, name) for name in (
            "pytest_collectreport", "pytest_deselected", "pytest_collection_finish",
            "pytest_runtest_logstart", "pytest_runtest_logreport", "pytest_sessionfinish",
        )],
    ]
    get = getattr

    def identity(owner, name):
        value = get(owner, name, None)
        value = get(value, "__func__", value)
        return value, get(value, "__code__", None)

    originals = [(owner, name, identity(owner, name)) for owner, name in pairs]

    def check():
        changed = []
        for owner, name, previous in originals:
            current = identity(owner, name)
            if current[0] is not previous[0] or current[1] is not previous[1]:
                changed.append(name)
        if changed:
            emit("runtime_violation", references=changed)

    return check


def main() -> int:
    descriptor, mode, root, *node_ids = sys.argv[1:]
    sys.argv[:] = [sys.argv[0]]
    descriptor = int(descriptor)
    os.set_inheritable(descriptor, False)
    if mode not in {"collect", "run"}:
        raise ValueError("invalid runner mode")
    with os.fdopen(descriptor, "w", encoding="utf-8", buffering=1) as output:
        # Capture encoding/writing functions before loading task code. These
        # references are still inspectable by arbitrary same-process Python.
        encode, write, flush = json.dumps, output.write, output.flush

        def emit(event, **fields):
            write(encode({"event": event, **fields}) + "\n")
            flush()

        reports = TestReports(emit)
        reports.check_references = _watch_runtime_references(reports, emit)
        emit("runner_start", version=2, mode=mode)
        sys.path.append(root)
        exitstatus = pytest.main(
            [
                "-c",
                os.devnull,
                "--rootdir",
                root,
                "--noconftest",
                "-p",
                "no:cacheprovider",
                "-q",
                *(["--collect-only"] if mode == "collect" else []),
                *node_ids,
            ],
            plugins=[reports],
        )
        reports.check_references()
        # Emit only after pytest returns, never from finally/atexit. The parent
        # also requires session_finish, complete per-test events, EOF and exit.
        emit("runner_complete", exitstatus=int(exitstatus))
    return int(exitstatus)


if __name__ == "__main__":
    raise SystemExit(main())
