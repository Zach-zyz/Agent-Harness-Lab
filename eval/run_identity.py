"""Deterministic identity for an HW2 episode input and model contract."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from harness.model_client import resolve_model_config


IGNORED_PARTS = {".git", ".pytest_cache", "__pycache__"}
ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = ("run_agent.py", "harness", "eval", "requirements.lock", "pyproject.toml", "Dockerfile")
NON_RUNTIME_ROOTS = {
    "config", "tasks", "tests", "tools", "runs", "outputs", "dist",
    "submission", "evidence", "sources",
}
MODEL_SETTINGS = ("base_url", "model_id", "action_mode", "reasoning_effort", "sampling")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def source_files(root: Path = ROOT) -> dict[str, str]:
    """Bind all shipped runtime code, including student extensions, without caches."""
    files = {}
    roots = set(SOURCE_PATHS)
    for child in root.iterdir():
        if child.name in NON_RUNTIME_ROOTS or child.name.startswith("."):
            continue
        if child.suffix == ".py" or (
            child.is_dir() and (child / "__init__.py").is_file()
        ):
            roots.add(child.name)
    for relative in sorted(roots):
        path = root / relative
        if path.is_symlink():
            raise ValueError(f"runtime source must not be a symlink: {relative}")
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in candidates:
            name = item.relative_to(root)
            if any(part in IGNORED_PARTS for part in name.parts) or item.suffix == ".pyc":
                continue
            if item.is_symlink():
                raise ValueError(f"runtime source must not be a symlink: {name}")
            if item.is_file():
                files[name.as_posix()] = sha256(item)
    return files


def runtime_identity() -> dict[str, Any]:
    dependencies = {}
    for name in ("pytest",):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "missing"
    return {
        "python": sys.version,
        "executable_sha256": sha256(Path(sys.executable)),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "dependencies": dependencies,
    }


def runtime_comparison_identity(runtime: Any) -> dict:
    """Ignore routine patch upgrades for matching; retain full per-run provenance."""
    if not isinstance(runtime, dict) or any(
        not isinstance(runtime.get(name), str) or not runtime[name].strip()
        for name in ("python", "implementation", "platform", "machine", "executable_sha256")
    ):
        raise ValueError("invalid model/provider/settings/runtime identity: missing runtime fields")
    python = runtime.get("python")
    match = re.fullmatch(r"(\d+\.\d+)\.\d+(?:(?:a|b|rc)\d+)?\+?(?:\s.*)?", python, re.DOTALL)
    dependencies = runtime.get("dependencies")
    if (
        match is None
        or re.fullmatch(r"[0-9a-f]{64}", runtime["executable_sha256"]) is None
        or not isinstance(dependencies, dict) or not dependencies
        or any(not isinstance(name, str) or not name.strip()
               or not isinstance(value, str) or not value.strip()
               for name, value in dependencies.items())
    ):
        raise ValueError("invalid model/provider/settings/runtime identity: version/hash/dependencies")
    return {
        "python": match.group(1),
        "implementation": runtime["implementation"],
        "platform": runtime["platform"].split("-", 1)[0],
        "machine": runtime["machine"],
        "dependencies": dependencies,
    }


def execution_identity(run: dict) -> dict:
    """Compare all experiment inputs, with a patch-tolerant runtime contract."""
    return {**run, "runtime": runtime_comparison_identity(run.get("runtime"))}


def comparison_identity(run: dict) -> dict:
    identity = {key: run.get(key) for key in ("backend", *MODEL_SETTINGS, "runtime")}
    # Legacy report-only records can omit run metadata; real endpoint episodes
    # must pass the full runtime validation in the runner/submission checker.
    if identity["runtime"] is not None:
        identity["runtime"] = runtime_comparison_identity(identity["runtime"])
    return identity


def task_input_sha256(task_dir: Path) -> str:
    entries: dict[str, dict[str, str | int]] = {}
    candidates = [task_dir / "task.json"]
    replay = task_dir / "replay.json"
    if replay.exists():
        candidates.append(replay)
    for directory_name in ("repo", "grader_tests"):
        directory = task_dir / directory_name
        if directory.is_dir():
            candidates.extend(directory.rglob("*"))

    for path in sorted(candidates):
        relative = path.relative_to(task_dir)
        if any(part in IGNORED_PARTS or part.endswith(".pyc") for part in relative.parts):
            continue
        logical_path = relative.as_posix()
        if path.is_symlink():
            entries[logical_path] = {
                "kind": "symlink",
                "target": os.readlink(path),
            }
        elif path.is_file():
            entries[logical_path] = {
                "kind": "file",
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }

    serialized = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_run_identity(
    *,
    task_dir: Path,
    seed: int,
    backend: str,
    config: Path,
    model_card: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    # Kept for callers from older releases; model documentation is not an input
    # contract and optional providers must not be pinned to a course model card.
    del model_card
    settings = json.loads(config.read_text(encoding="utf-8"))
    if backend == "endpoint":
        provider = resolve_model_config()
        model = {
            "model_id": provider.model_id,
            "base_url": provider.base_url,
            "action_mode": provider.action_mode,
            "reasoning_effort": provider.reasoning_effort,
        }
    else:
        model = {
            "model_id": "deterministic-replay", "base_url": None,
            "action_mode": "replay", "reasoning_effort": None,
        }
    files = source_files(source_root or ROOT)
    return {
        "identity_version": 2,
        "seed": seed,
        "backend": backend,
        "config_sha256": sha256(config),
        "config": settings,
        "task_input_sha256": task_input_sha256(task_dir),
        **model,
        "sampling": {
            "temperature": settings.get("temperature", 0.2),
            "top_p": settings.get("top_p", 1.0),
            "max_output_tokens": settings.get("max_output_tokens", 2048),
        },
        "source_files": files,
        "source_sha256": object_sha256(files),
        "runtime": runtime_identity(),
    }
