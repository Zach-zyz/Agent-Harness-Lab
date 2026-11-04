"""Real OS probes: these deliberately execute code outside the AST policy."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import signal
import threading

import pytest

from harness.sandbox import (
    Limits, MAX_CAPTURE_BYTES, REAL_POPEN, Sandbox, SandboxUnavailable,
)


def python(worktree, source, **options):
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", source], cwd=worktree,
        capture_output=True, timeout=15, **options,
    )


def test_real_files_network_process_and_environment_isolation(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "host-canary"
    outside.write_text("disposable host secret")
    (worktree / "escape").symlink_to(outside)
    monkeypatch.setenv("HW2_MODEL_API_KEY", "disposable-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "disposable-cloud-key")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("LD_PRELOAD", "/nonexistent/host-injection")
    source = f"""
import errno, json, os, pathlib, socket, subprocess, sys
checks = {{}}
def denied(name, operation):
    try:
        operation()
        checks[name] = False
    except OSError as error:
        checks[name] = error.errno in (errno.EPERM, errno.EACCES, errno.ENOENT, errno.EROFS)
outside = pathlib.Path({str(outside)!r})
denied("outside_read", outside.read_text)
denied("outside_write", lambda: outside.write_text("changed"))
denied("symlink_escape", lambda: pathlib.Path("escape").read_text())
denied("network", lambda: socket.socket().connect(("127.0.0.1", 9)))
denied("subprocess", lambda: subprocess.run([sys.executable, "-c", "pass"], check=True))
try:
    pid = os.fork()
except OSError as error:
    checks["fork"] = error.errno in (errno.EPERM, errno.EACCES)
else:
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    checks["fork"] = False
pathlib.Path("allowed").write_text("ok")
checks["worktree_write"] = pathlib.Path("allowed").read_text() == "ok"
checks["cleanenv"] = all(key not in os.environ for key in
    ("HW2_MODEL_API_KEY", "AWS_SECRET_ACCESS_KEY", "LD_PRELOAD"))
if sys.platform.startswith("linux"):
    checks["host_pid_hidden"] = not pathlib.Path("/proc/{os.getpid()}").exists()
print(json.dumps(checks))
assert all(checks.values()), checks
"""
    with Sandbox(worktree):
        result = python(worktree, source)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())
    assert outside.read_text() == "disposable host secret"


def test_exact_trusted_read_does_not_expose_siblings_or_allow_writes(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    trusted = tmp_path / "course" / "staff" / "runner.py"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("trusted runtime")
    sibling = trusted.with_name("unrelated-secret")
    sibling.write_text("disposable")
    with Sandbox(worktree, read_files=(trusted,)):
        result = python(worktree, f"""
from pathlib import Path
assert Path({str(trusted)!r}).read_text() == "trusted runtime"
for operation in (
    lambda: Path({str(sibling)!r}).read_text(),
    lambda: Path({str(trusted)!r}).write_text("tampered"),
):
    try:
        operation()
    except OSError:
        pass
    else:
        raise AssertionError("outside access succeeded")
""")
    assert result.returncode == 0, result.stderr
    assert trusted.read_text() == "trusted runtime"


def test_trusted_report_fd_survives_but_other_host_fd_does_not(tmp_path):
    # Deliberately make the reporting descriptor non-contiguous/high.
    fillers = [os.open(os.devnull, os.O_RDONLY) for _ in range(8)]
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(fillers[0], True)
        with Sandbox(tmp_path):
            result = python(tmp_path, f"""
import os
try:
    os.fstat({fillers[0]})
except OSError:
    pass
else:
    raise AssertionError("inherited a non-reporting host descriptor")
os.write({write_fd}, b"trusted-pipe-event\\n")
""", pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = -1
        assert result.returncode == 0, result.stderr
        assert os.read(read_fd, 1000) == b"trusted-pipe-event\n"
    finally:
        for descriptor in [*fillers, read_fd, write_fd]:
            if descriptor >= 0:
                os.close(descriptor)


def test_output_flood_is_stopped_and_capture_is_bounded(tmp_path):
    with Sandbox(tmp_path):
        result = python(tmp_path, "import os\nwhile True: os.write(1, b'x' * 65536)")
    assert result.returncode != 0
    assert len(result.stdout) + len(result.stderr) <= MAX_CAPTURE_BYTES
    assert b"output exceeded" in result.stderr


def test_memory_growth_is_stopped(tmp_path):
    with Sandbox(tmp_path):
        result = python(
            tmp_path, "import time\nitems=[]\nwhile True:\n"
            " items.append(bytearray(4*1024*1024))\n time.sleep(.002)",
        )
    assert result.returncode != 0
    assert b"memory exceeded" in result.stderr or b"MemoryError" in result.stderr


def test_wall_clock_and_cpu_bounds(tmp_path):
    with Sandbox(tmp_path, limits=Limits(wall_seconds=.5)):
        start = time.monotonic()
        result = python(tmp_path, "import time; time.sleep(30)")
    assert time.monotonic() - start < 5
    assert result.returncode != 0
    assert b"wall-clock limit" in result.stderr
    with Sandbox(tmp_path, limits=Limits(cpu_seconds=1, wall_seconds=5)):
        start = time.monotonic()
        result = python(tmp_path, "while True: pass")
    assert time.monotonic() - start < 4.5
    assert b"wall-clock limit" not in result.stderr
    assert result.returncode in (
        -signal.SIGKILL, -signal.SIGXCPU, 128 + signal.SIGKILL, 128 + signal.SIGXCPU,
    )


def test_git_works_and_blocked_git_is_bounded(tmp_path):
    with Sandbox(tmp_path):
        for args in (
            ["init", "-q"], ["config", "user.email", "test@example.invalid"],
            ["config", "user.name", "Sandbox test"], ["add", "."],
            ["commit", "--allow-empty", "-qm", "base"],
        ):
            subprocess.run(["git", *args], cwd=tmp_path, check=True)
    head = tmp_path / ".git" / "HEAD"
    head.unlink()
    os.mkfifo(head)
    with Sandbox(tmp_path, limits=Limits(wall_seconds=.5)):
        start = time.monotonic()
        result = subprocess.run(["git", "status"], cwd=tmp_path, capture_output=True)
    assert time.monotonic() - start < 5
    assert result.returncode != 0
    assert b"wall-clock limit" in result.stderr


def test_context_decorator_and_nested_scope_restore_popen(tmp_path):
    @Sandbox(tmp_path)
    def run():
        with Sandbox(tmp_path):
            return python(tmp_path, "print('ok')")
    assert run().stdout == b"ok\n"
    assert run().stdout == b"ok\n"
    assert subprocess.Popen is REAL_POPEN


def test_concurrent_context_fails_closed_without_corrupting_owner(tmp_path):
    errors = []
    def concurrent():
        try:
            with Sandbox(tmp_path):
                errors.append("incorrectly entered")
        except SandboxUnavailable as error:
            errors.append(str(error))
    scope = Sandbox(tmp_path)
    with scope:
        thread = threading.Thread(target=concurrent)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert errors == ["Concurrent sandbox contexts are unsupported"]
        with pytest.raises(SandboxUnavailable, match="fresh Sandbox"):
            with scope:
                pytest.fail("same instance cannot be nested")
        assert python(tmp_path, "print('owner ok')").stdout == b"owner ok\n"
    assert subprocess.Popen is REAL_POPEN


def test_missing_backend_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("harness.sandbox.shutil.which", lambda *a, **kw: None)
    with pytest.raises(SandboxUnavailable, match="apt install bubblewrap"):
        with Sandbox(tmp_path):
            pytest.fail("must not enter context")
    assert subprocess.Popen is REAL_POPEN


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Mach/Keychain service")
def test_macos_keychain_mach_service_is_denied(tmp_path):
    # Request a service port only; never query or retrieve a real credential.
    import ctypes
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    bootstrap = ctypes.c_uint.in_dll(library, "bootstrap_port").value
    port = ctypes.c_uint()
    assert library.bootstrap_look_up(
        bootstrap, b"com.apple.SecurityServer", ctypes.byref(port),
    ) == 0, "Keychain service must be reachable in the host positive control"
    with Sandbox(tmp_path):
        result = python(tmp_path, """
import ctypes
lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
bootstrap = ctypes.c_uint.in_dll(lib, "bootstrap_port").value
port = ctypes.c_uint()
status = lib.bootstrap_look_up(bootstrap, b"com.apple.SecurityServer", ctypes.byref(port))
assert status != 0, "Keychain Mach service accessible"
""")
    assert result.returncode == 0, result.stderr
