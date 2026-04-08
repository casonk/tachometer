from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .model import ResourceSnapshot

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tachometer",
    "build",
    "dist",
    "node_modules",
}


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return out
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.strip().split()
        if not parts:
            continue
        with contextlib.suppress(ValueError):
            out[key] = int(parts[0]) * 1024
    return out


def _cpu_percent_sample(sample_seconds: float = 0.25) -> float | None:
    stat = Path("/proc/stat")
    if not stat.exists():
        return None

    def read_cpu() -> tuple[int, int]:
        line = stat.read_text(encoding="utf-8").splitlines()[0]
        parts = [int(piece) for piece in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return idle, total

    idle1, total1 = read_cpu()
    time.sleep(sample_seconds)
    idle2, total2 = read_cpu()
    delta_total = total2 - total1
    delta_idle = idle2 - idle1
    if delta_total <= 0:
        return None
    return round(100.0 * (1 - delta_idle / delta_total), 2)


def _gpu_snapshot() -> dict[str, Any]:
    if shutil.which("nvidia-smi") is None:
        return {"gpu_detected": False}
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"gpu_detected": True}
        first = proc.stdout.strip().splitlines()[0]
        name, util, mem_used, mem_total = [value.strip() for value in first.split(",", 3)]
        return {
            "gpu_detected": True,
            "gpu_name": name,
            "gpu_util_percent": float(util),
            "gpu_mem_used_mb": float(mem_used),
            "gpu_mem_total_mb": float(mem_total),
        }
    except Exception:
        return {"gpu_detected": True}


def _git_metrics(repo_root: Path) -> dict[str, int | None]:
    if shutil.which("git") is None:
        return {}

    def run_git(*args: str) -> list[str] | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return [line for line in proc.stdout.splitlines() if line.strip()]

    tracked = run_git("ls-files")
    non_ignored = run_git("ls-files", "--cached", "--others", "--exclude-standard")
    status_lines = run_git("status", "--short", "--untracked-files=all")
    if tracked is None and status_lines is None:
        return {}

    dirty_count = None
    untracked_count = None
    if status_lines is not None:
        dirty_count = 0
        untracked_count = 0
        for line in status_lines:
            if line.startswith("?? "):
                untracked_count += 1
            else:
                dirty_count += 1

    def _sum_sizes(paths: list[str] | None) -> int | None:
        if paths is None:
            return None
        total = 0
        for rel in paths:
            with contextlib.suppress(OSError):
                total += (repo_root / rel).stat().st_size
        return total

    return {
        "git_tracked_file_count": len(tracked) if tracked is not None else None,
        "git_dirty_file_count": dirty_count,
        "git_untracked_file_count": untracked_count,
        "git_tracked_size_bytes": _sum_sizes(tracked),
        "git_non_ignored_size_bytes": _sum_sizes(non_ignored),
    }


def _repo_metrics(repo_root: str | Path | None) -> dict[str, Any]:
    if repo_root is None:
        return {}
    root = Path(repo_root).resolve()
    if not root.exists():
        return {"repo_root": str(root)}

    file_count = 0
    dir_count = 0
    total_size = 0
    for walk_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        dir_count += len(dirnames)
        for filename in filenames:
            file_count += 1
            path = Path(walk_root) / filename
            with contextlib.suppress(OSError):
                total_size += path.stat().st_size

    metrics: dict[str, Any] = {
        "repo_root": str(root),
        "repo_file_count": file_count,
        "repo_dir_count": dir_count,
        "repo_size_bytes": total_size,
    }
    metrics.update(_git_metrics(root))
    return metrics


def collect_resource_snapshot(
    path: str | Path = "/", repo_root: str | Path | None = None
) -> ResourceSnapshot:
    mem = _read_meminfo()
    disk = shutil.disk_usage(path)
    load1, load5, load15 = (None, None, None)
    with contextlib.suppress(Exception):
        load1, load5, load15 = os.getloadavg()

    base = {
        "timestamp": time.time(),
        "cpu_percent": _cpu_percent_sample(),
        "loadavg_1m": load1,
        "loadavg_5m": load5,
        "loadavg_15m": load15,
        "memory_total_bytes": mem.get("MemTotal"),
        "memory_available_bytes": mem.get("MemAvailable"),
        "memory_used_bytes": (
            (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) if mem else None
        ),
        "disk_total_bytes": disk.total,
        "disk_used_bytes": disk.used,
        "disk_free_bytes": disk.free,
    }
    base.update(_gpu_snapshot())
    base.update(_repo_metrics(repo_root))
    return ResourceSnapshot(**base)


def _load_profile_document(profile_path: Path) -> dict[str, Any]:
    if not profile_path.exists():
        return {"samples": [], "runs": []}
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    data.setdefault("samples", [])
    data.setdefault("runs", [])
    return data


def append_profile_sample(
    profile_path: str | Path,
    payload: dict[str, Any],
    *,
    repo_metadata: dict[str, Any] | None = None,
) -> None:
    profile_path = Path(profile_path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_profile_document(profile_path)
    if repo_metadata and "repo" not in data:
        data["repo"] = repo_metadata
    data["samples"].append(payload)
    profile_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_run_record(
    profile_path: str | Path,
    payload: dict[str, Any],
    *,
    repo_metadata: dict[str, Any] | None = None,
) -> None:
    profile_path = Path(profile_path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_profile_document(profile_path)
    if repo_metadata and "repo" not in data:
        data["repo"] = repo_metadata
    data["runs"].append(payload)
    profile_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _avg(samples: list[dict[str, Any]], key: str) -> float | None:
    values = [sample.get(key) for sample in samples if isinstance(sample.get(key), (int, float))]
    return round(sum(values) / len(values), 3) if values else None


def _max(samples: list[dict[str, Any]], key: str) -> float | None:
    values = [sample.get(key) for sample in samples if isinstance(sample.get(key), (int, float))]
    return max(values, default=None)


def _latest_numeric(samples: list[dict[str, Any]], key: str) -> int | float | None:
    for sample in reversed(samples):
        value = sample.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def summarize_samples(profile_path: str | Path) -> dict[str, Any]:
    profile_path = Path(profile_path)
    if not profile_path.exists():
        return {"sample_count": 0}

    data = _load_profile_document(profile_path)
    samples = data.get("samples", [])
    if not samples:
        return {"sample_count": 0}

    return {
        "sample_count": len(samples),
        "avg_cpu_percent": _avg(samples, "cpu_percent"),
        "avg_memory_used_bytes": _avg(samples, "memory_used_bytes"),
        "avg_disk_used_bytes": _avg(samples, "disk_used_bytes"),
        "avg_gpu_util_percent": _avg(samples, "gpu_util_percent"),
        "avg_repo_size_bytes": _avg(samples, "repo_size_bytes"),
        "max_cpu_percent": _max(samples, "cpu_percent"),
        "max_gpu_util_percent": _max(samples, "gpu_util_percent"),
        "latest_memory_total_bytes": _latest_numeric(samples, "memory_total_bytes"),
        "latest_disk_total_bytes": _latest_numeric(samples, "disk_total_bytes"),
        "latest_gpu_mem_total_mb": _latest_numeric(samples, "gpu_mem_total_mb"),
        "latest_repo_size_bytes": _latest_numeric(samples, "repo_size_bytes"),
        "latest_repo_file_count": _latest_numeric(samples, "repo_file_count"),
        "latest_repo_dir_count": _latest_numeric(samples, "repo_dir_count"),
        "latest_git_tracked_file_count": _latest_numeric(samples, "git_tracked_file_count"),
        "latest_git_dirty_file_count": _latest_numeric(samples, "git_dirty_file_count"),
        "latest_git_untracked_file_count": _latest_numeric(samples, "git_untracked_file_count"),
        "latest_git_tracked_size_bytes": _latest_numeric(samples, "git_tracked_size_bytes"),
        "latest_git_non_ignored_size_bytes": _latest_numeric(samples, "git_non_ignored_size_bytes"),
    }


def write_summary(summary_path: str | Path, summary: dict[str, Any]) -> None:
    summary_path = Path(summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def run_profiled_command(
    *,
    name: str,
    command: list[str],
    profile_path: str | Path,
    repo_root: str | Path | None = None,
    path: str | Path = "/",
    cwd: str | Path | None = None,
    repo_metadata: dict[str, Any] | None = None,
    capture_output_bytes: int = 4000,
) -> dict[str, Any]:
    start_snapshot = collect_resource_snapshot(path=path, repo_root=repo_root)
    append_profile_sample(
        profile_path,
        {"name": name, "phase": "pre", **asdict(start_snapshot)},
        repo_metadata=repo_metadata,
    )

    started = time.time()
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    runtime = round(time.time() - started, 3)

    end_snapshot = collect_resource_snapshot(path=path, repo_root=repo_root)
    append_profile_sample(
        profile_path,
        {
            "name": name,
            "phase": "post",
            "runtime_seconds": runtime,
            **asdict(end_snapshot),
        },
        repo_metadata=repo_metadata,
    )

    summary = summarize_samples(profile_path)
    record = {
        "name": name,
        "command": command,
        "cwd": str(cwd) if cwd else None,
        "returncode": proc.returncode,
        "runtime_seconds": runtime,
        "stdout_tail": proc.stdout[-capture_output_bytes:] if capture_output_bytes else "",
        "stderr_tail": proc.stderr[-capture_output_bytes:] if capture_output_bytes else "",
        "summary": summary,
    }
    append_run_record(profile_path, record, repo_metadata=repo_metadata)
    return record
