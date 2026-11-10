"""Fail-closed OS isolation for generated code and Git inspecting its worktree.

Use ``with Sandbox(worktree):`` (also a ContextDecorator) around subprocess
calls, or ``@sandboxed`` when the function's first argument is its worktree.
The host keeps credentials; children get only runtime reads, their worktree and
private scratch. Contexts are synchronous and may nest, but cannot span threads.
Popen callers that drain pipes themselves must call account_output for EVERY
byte (including trusted event pipes); communicate does this automatically.

Linux/WSL2 requires bubblewrap, libseccomp and enabled unprivileged namespaces.
There is deliberately no unsandboxed or Docker fallback.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import json
import math
import os
from pathlib import Path
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from dataclasses import dataclass


MAX_CAPTURE_BYTES = 1024 * 1024
MAX_RESIDENT_BYTES = 256 * 1024 * 1024
MAX_PROCESS_SECONDS = 60
REAL_POPEN = subprocess.Popen
RUNNER = Path(__file__).resolve().parents[1] / "eval" / "_pytest_runner.py"
_active = None
_context_lock = threading.RLock()


class SandboxUnavailable(RuntimeError):
    """The required isolation cannot be established; execution is refused."""


LINUX_REQUIREMENTS = (
    "Linux/WSL2 requires bubblewrap and libseccomp2 "
    "(sudo apt install bubblewrap libseccomp2), kernel seccomp support, and "
    "permission to create unprivileged user/network namespaces. On Ubuntu "
    "24.04, the AppArmor profile for /usr/bin/bwrap must permit userns. "
    "Refusing unsandboxed execution."
)


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = MAX_PROCESS_SECONDS
    memory_bytes: int = MAX_RESIDENT_BYTES
    output_bytes: int = MAX_CAPTURE_BYTES
    cpu_seconds: int = MAX_PROCESS_SECONDS

    def __post_init__(self):
        for name, maximum in (
            ("wall_seconds", MAX_PROCESS_SECONDS),
            ("memory_bytes", MAX_RESIDENT_BYTES),
            ("output_bytes", MAX_CAPTURE_BYTES),
            ("cpu_seconds", MAX_PROCESS_SECONDS),
        ):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or (name != "wall_seconds" and not isinstance(value, int))
                    or not 0 < value <= maximum or not math.isfinite(value)):
                raise ValueError(f"{name} must be in (0, {maximum}]")


def sandboxed(function):
    """Sandbox a function whose first argument is its worktree."""
    @functools.wraps(function)
    def wrapped(worktree, *args, **kwargs):
        with Sandbox(worktree):
            return function(worktree, *args, **kwargs)
    return wrapped


def _runtime_paths():
    # Never expose sys.prefix wholesale: a venv can live inside the course/home.
    trees = {Path(sysconfig.get_path(key)).resolve()
             for key in ("stdlib", "platstdlib", "purelib", "platlib")}
    trees.add(Path(sys.base_prefix).resolve() / "lib")
    files = {Path(sys.executable).absolute(), Path(sys.executable).resolve(),
             Path(sys.prefix) / "pyvenv.cfg"}
    git = shutil.which("git", path=os.defpath + ":/opt/homebrew/bin:/usr/local/bin")
    # Prefer a real Homebrew git to Apple's developer-tool launcher.
    if sys.platform == "darwin" and Path("/opt/homebrew/bin/git").exists():
        git = "/opt/homebrew/bin/git"
    if git:
        files.update((Path(git), Path(git).resolve()))
    if sys.platform == "darwin":
        trees.update(map(Path, ("/System/Library", "/usr/lib")))
        framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
        framework_prefix = sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")
        if framework and framework_prefix:
            trees.add((Path(framework_prefix) / f"{framework}.framework").resolve())
        # Homebrew's runtime store contains libraries, not user documents.
        # Restrict grants to installed package lib directories (not all Cellar).
        trees.update(path.resolve() for path in Path("/opt/homebrew/opt").glob("*/lib"))
        if git and "/Cellar/git/" in str(Path(git).resolve()):
            trees.add(Path(git).resolve().parents[1] / "share")
    else:
        trees.update(path for path in map(Path, (
            "/lib", "/lib64", "/usr/lib", "/usr/lib64",
        )) if path.exists())
        files.update(path for path in map(Path, (
            "/etc/ld.so.cache", "/etc/localtime", "/usr/share/git-core/templates",
        )) if path.is_file())
    return {p for p in trees if p.is_dir()}, {p for p in files if p.exists()}, git


def _seccomp_file():
    """Export architecture-native BPF; bwrap applies it AFTER its own setup."""
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as error:
        raise SandboxUnavailable(
            "Linux sandbox requires libseccomp (Ubuntu/WSL2: sudo apt install "
            "bubblewrap libseccomp2). Refusing unsandboxed execution."
        ) from error
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise SandboxUnavailable("Cannot initialize libseccomp; refusing execution")
    handle = tempfile.TemporaryFile()
    try:
        for name in (
            "clone", "clone3", "fork", "vfork", "unshare", "setns",
            "socket", "socketpair", "connect", "ptrace", "process_vm_readv",
            "process_vm_writev", "mount", "umount2", "pivot_root", "chroot",
            "bpf", "perf_event_open", "keyctl", "add_key", "request_key",
            "io_uring_setup", "io_uring_enter", "io_uring_register",
            "open_by_handle_at", "pidfd_getfd", "userfaultfd",
        ):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(
                ctx, 0x00050000 | errno.EPERM, number, 0,
            ):
                raise SandboxUnavailable(f"Cannot restrict Linux syscall {name}")
        if lib.seccomp_export_bpf(ctx, handle.fileno()):
            raise SandboxUnavailable("Cannot export seccomp filter")
        handle.seek(0)
        return handle
    except BaseException:
        handle.close()
        raise
    finally:
        lib.seccomp_release(ctx)


class Sandbox(contextlib.ContextDecorator):
    def __init__(self, worktree, *, read_files=(), limits=None):
        self.worktree = Path(worktree).resolve()
        self.read_files = {Path(p).resolve() for p in read_files} | {RUNNER.resolve()}
        self.limits = limits or Limits()

    def _recreate_cm(self):
        return type(self)(self.worktree, read_files=self.read_files, limits=self.limits)

    def __enter__(self):
        if not _context_lock.acquire(blocking=False):
            raise SandboxUnavailable("Concurrent sandbox contexts are unsupported")
        try:
            if getattr(self, "_entered", False):
                raise SandboxUnavailable("Use a fresh Sandbox instance for nested contexts")
            return self._enter_scope()
        except BaseException:
            _context_lock.release()
            raise

    def _enter_scope(self):
        global _active
        self.previous = _active
        self.owner = threading.get_ident()
        if _active is not None and _active.owner != self.owner:
            raise SandboxUnavailable("Concurrent sandbox contexts are unsupported")
        if subprocess.Popen not in (REAL_POPEN, SandboxedPopen):
            raise SandboxUnavailable("Unexpected subprocess wrapper; refusing execution")
        if self.previous is not None:
            self.limits = Limits(**{
                name: min(getattr(self.limits, name), getattr(self.previous.limits, name))
                for name in ("wall_seconds", "memory_bytes", "output_bytes", "cpu_seconds")
            })
        if sys.platform == "darwin":
            self.backend = "/usr/bin/sandbox-exec"
            if not Path(self.backend).is_file():
                raise SandboxUnavailable("macOS requires /usr/bin/sandbox-exec")
        elif sys.platform.startswith("linux"):
            self.backend = shutil.which("bwrap")
            if not self.backend:
                raise SandboxUnavailable(LINUX_REQUIREMENTS)
        else:
            raise SandboxUnavailable("Use macOS or Linux/WSL2 with bubblewrap")
        self.trees, self.files, self.git = _runtime_paths()
        for path in self.read_files:
            if not path.is_file():
                raise SandboxUnavailable(f"Trusted read must be an exact regular file: {path}")
        self.files.update(self.read_files)
        self._temp = tempfile.TemporaryDirectory(prefix="hw2-sandbox-")
        self.scratch = Path(self._temp.name).resolve()
        (self.scratch / "home").mkdir()
        (self.scratch / "tmp").mkdir()
        self.processes = []
        _active = self
        subprocess.Popen = SandboxedPopen
        self._entered = True
        return self

    def __exit__(self, *exc):
        global _active
        try:
            for process in self.processes:
                if process.poll() is None:
                    process.stop_for_limit("sandbox context closed")
                process.wait()
        finally:
            _active = self.previous
            subprocess.Popen = SandboxedPopen if _active else REAL_POPEN
            self._entered = False
            try:
                self._temp.cleanup()
            finally:
                _context_lock.release()

    def environment(self, incoming):
        # Do not inherit PATH/PYTHONPATH, loader options, Git config, or secrets.
        env = {key: incoming[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
               if key in incoming}
        env.update({
            "PATH": os.defpath, "HOME": str(self.scratch / "home"),
            "TMPDIR": str(self.scratch / "tmp"), "TEMP": str(self.scratch / "tmp"),
            "TMP": str(self.scratch / "tmp"), "PYTHONPATH": str(self.worktree),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "PYTHONHASHSEED": "0", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
            "GIT_ATTR_NOSYSTEM": "1",
        })
        return env

    def macos_profile(self):
        files = self.files | set(map(Path, (
            "/", "/dev/null", "/dev/urandom", "/dev/random", "/private/etc/localtime",
        )))
        trees = self.trees | {self.worktree, self.scratch}
        # Pytest may list ancestor directory nodes, never their contents' files.
        for path in trees | files:
            files.update(path.parents)
        quote = lambda p: json.dumps(str(p))
        return "\n".join((
            "(version 1)", "(deny default)", "(allow process-exec)",
            "(allow sysctl-read)", "(allow file-read-metadata)",
            "(allow file-read* "
            + " ".join(f"(subpath {quote(p)})" for p in sorted(trees)) + " "
            + " ".join(f"(literal {quote(p)})" for p in sorted(files)) + ")",
            f"(allow file-write* (subpath {quote(self.worktree)}) "
            f"(subpath {quote(self.scratch)}) (literal \"/dev/null\"))",
        ))

    def linux_command(self, command, seccomp_fd):
        result = [
            self.backend, "--unshare-all", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL", "--clearenv",
            "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
        ]
        # Construct an empty root. Only these trusted runtime paths are mounted.
        mounts = self.trees | self.files
        for path in sorted(mounts, key=lambda p: (len(p.parts), str(p))):
            if any(parent in self.trees for parent in path.parents):
                continue
            result.extend(("--ro-bind", str(path), str(path)))
        result.extend((
            "--bind", str(self.worktree), str(self.worktree),
            "--bind", str(self.scratch), str(self.scratch),
            # Ancestor scaffolding is not writable. TMPDIR names the private
            # writable scratch mount; the fresh /tmp reveals no host temp data.
            "--remount-ro", "/", "--remount-ro", "/tmp",
            "--remount-ro", "/proc", "--remount-ro", "/dev",
            "--chdir", str(self.worktree), "--seccomp", str(seccomp_fd),
        ))
        for key, value in self.environment({}).items():
            result.extend(("--setenv", key, value))
        # bwrap forwards inherited descriptors to the executed command (and
        # closes them in its monitor/init). Popen's pass_fds is the allowlist;
        # the seccomp descriptor is consumed and closed by bwrap itself.
        return [*result, "--", *command]


class SandboxedPopen(REAL_POPEN):
    def __init__(self, args, *positional, **options):
        scope = _active
        if scope is None or scope.owner != threading.get_ident():
            raise SandboxUnavailable("Subprocess launched outside its sandbox context")
        if (positional or options.get("shell") or options.get("executable")
                or options.get("preexec_fn") or options.get("close_fds") is False
                or not isinstance(args, (list, tuple)) or not args or not options.get("cwd")):
            raise SandboxUnavailable("Unsupported sandbox subprocess invocation")
        cwd = Path(options["cwd"]).resolve()
        if cwd != scope.worktree and scope.worktree not in cwd.parents:
            raise SandboxUnavailable("Subprocess cwd must be inside its worktree")
        command = list(map(str, args))
        if Path(command[0]).name == "git":
            if not scope.git:
                raise SandboxUnavailable("Git is required")
            command[0] = str(Path(scope.git).resolve())
            command[1:1] = [
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
                "-c", "diff.external=", "-c", "protocol.allow=never",
            ]
        elif command[0] == sys.executable:
            # The real binary still detects pyvenv.cfg via the original path.
            pass
        else:
            raise SandboxUnavailable("Only the trusted Python and Git executables may launch")
        options["env"] = scope.environment(options.get("env") or {})
        options["stdin"] = subprocess.DEVNULL
        for channel in ("stdout", "stderr"):
            if options.get(channel) not in (None, subprocess.PIPE, subprocess.STDOUT):
                raise SandboxUnavailable("Sandbox output must use bounded pipes")
            options[channel] = options.get(channel) or subprocess.PIPE
        options["start_new_session"] = True
        options["close_fds"] = True
        self.limits = scope.limits
        self.resource_reason = ""
        self._output_used = 0
        self._capture = {"stdout": bytearray(), "stderr": bytearray()}
        seccomp = None
        passed_fds = tuple(options.get("pass_fds", ()))
        if any(fd < 3 for fd in passed_fds):
            raise SandboxUnavailable("Only explicit nonstandard report descriptors may pass")
        try:
            if sys.platform == "darwin":
                command = [scope.backend, "-p", scope.macos_profile(), *command]
                self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
                self._libproc.proc_pidinfo.argtypes = [
                    ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
                ]
            else:
                seccomp = _seccomp_file()
                options["pass_fds"] = (*passed_fds, seccomp.fileno())
                command = scope.linux_command(command, seccomp.fileno())
            options["preexec_fn"] = self._resource_limits
            super().__init__(command, **options)
        finally:
            if seccomp is not None:
                seccomp.close()
        scope.processes.append(self)
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _resource_limits(self):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(self.limits.cpu_seconds),) * 2)
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (self.limits.memory_bytes,) * 2)

    def stop_for_limit(self, reason):
        if self.resource_reason:
            return
        self.resource_reason = reason
        try:
            if sys.platform == "darwin":
                # Seatbelt forbids forks. Signalling its dying process group
                # repeatedly can return EPERM on macOS; Popen.kill also reaps
                # before signalling to avoid signalling a recycled PID.
                super().kill()
            elif self.poll() is None:
                os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def account_output(self, size):
        available = max(0, self.limits.output_bytes - self._output_used)
        self._output_used += size
        if size > available:
            self.stop_for_limit("subprocess output exceeded limit")
        return min(size, available)

    def check_backend(self, stderr):
        if (sys.platform.startswith("linux") and self.returncode
                and stderr.startswith(b"bwrap:")):
            raise SandboxUnavailable(
                LINUX_REQUIREMENTS + "\n" + stderr[:2000].decode("utf-8", errors="replace")
            )

    def _watch(self):
        deadline = time.monotonic() + self.limits.wall_seconds
        while self.poll() is None:
            if sys.platform == "darwin":
                buffer = ctypes.create_string_buffer(96)
                size = self._libproc.proc_pidinfo(self.pid, 4, 0, buffer, 96)
                if size != 96:
                    # Exiting processes can disappear from libproc just before
                    # waitpid observes their exit. Reap that race; an actually
                    # live, unmonitorable process still fails closed.
                    try:
                        super().wait(timeout=0.025)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                    if self.poll() is None:
                        self.stop_for_limit("memory monitoring unavailable")
                    return
                resident = int.from_bytes(buffer.raw[8:16], sys.byteorder)
                if resident > self.limits.memory_bytes:
                    self.stop_for_limit("resident memory exceeded limit")
                    return
            if time.monotonic() >= deadline:
                self.stop_for_limit("subprocess exceeded wall-clock limit")
                return
            time.sleep(0.005)

    def wait(self, timeout=None):
        result = super().wait(timeout=timeout)
        watcher = getattr(self, "_watcher", None)
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=1)
        return result

    def communicate(self, input=None, timeout=None):
        if input is not None:
            raise SandboxUnavailable("Sandbox stdin payloads are unsupported")
        deadline = time.monotonic() + min(timeout or self.limits.wall_seconds,
                                          self.limits.wall_seconds)
        with selectors.DefaultSelector() as channels:
            for name in ("stdout", "stderr"):
                pipe = getattr(self, name)
                if pipe is not None and not pipe.closed:
                    channels.register(pipe, selectors.EVENT_READ, name)
            while channels.get_map():
                if time.monotonic() >= deadline:
                    self.stop_for_limit("subprocess exceeded wall-clock limit")
                for selected, _ in channels.select(0.05):
                    chunk = os.read(selected.fd, 65536)
                    if not chunk:
                        channels.unregister(selected.fileobj)
                        selected.fileobj.close()
                        continue
                    allowed = self.account_output(len(chunk))
                    self._capture[selected.data].extend(chunk[:allowed])
        self.wait()
        self.check_backend(bytes(self._capture["stderr"]))
        if self.resource_reason:
            # Replace the tail so the diagnostic ALSO fits the aggregate cap.
            note = ("\nSandbox stopped: " + self.resource_reason + "\n").encode()
            for name in ("stdout", "stderr"):
                excess = sum(map(len, self._capture.values())) + len(note) - self.limits.output_bytes
                if excess > 0:
                    del self._capture[name][max(0, len(self._capture[name]) - excess):]
            self._capture["stderr"].extend(note[:self.limits.output_bytes])
        return tuple(
            None if getattr(self, name) is None else (
                bytes(self._capture[name]).decode(self.encoding or "utf-8", errors="replace")
                if self.text_mode else bytes(self._capture[name])
            ) for name in ("stdout", "stderr")
        )
