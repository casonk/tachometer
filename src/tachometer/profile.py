from __future__ import annotations

import contextlib
import json
import os
import resource
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .model import ResourceSnapshot

try:
    import psutil as _psutil  # type: ignore[import-not-found]

    _PSUTIL_AVAILABLE = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False

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


def _monitor_process(
    pid: int,
    samples: list[dict[str, float]],
    stop: threading.Event,
    interval: float = 0.25,
) -> None:
    """Background thread: sample CPU and RSS of a process tree via psutil.

    cpu_percent values are normalised by logical CPU count so they represent
    0–100 % of total system capacity rather than per-core percentages.
    """
    if not _PSUTIL_AVAILABLE or _psutil is None:
        return
    cpu_count = _psutil.cpu_count(logical=True) or 1
    try:
        parent = _psutil.Process(pid)
        # Primer call — establishes cpu_percent baseline; always returns 0.0, discard.
        parent.cpu_percent(interval=None)
        for child in parent.children(recursive=True):
            with contextlib.suppress(_psutil.NoSuchProcess, _psutil.AccessDenied):
                child.cpu_percent(interval=None)
        while True:
            # stop.wait is interruptible; returns True when stop fires, False on timeout.
            stop_fired = stop.wait(interval)
            # Always collect one sample — this guarantees data for short-lived processes.
            try:
                procs = [parent] + parent.children(recursive=True)
                raw_cpu = sum(p.cpu_percent(interval=None) for p in procs if p.is_running())
                cpu = raw_cpu / cpu_count  # normalise to 0–100 % of total capacity
                rss = sum(p.memory_info().rss for p in procs if p.is_running())
                samples.append({"cpu_percent": cpu, "memory_rss_bytes": rss})
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                break
            if stop_fired:
                break
    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
        pass


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


def collect_host_resource_snapshot(path: str | Path = "/") -> ResourceSnapshot:
    return collect_resource_snapshot(path=path, repo_root=None)


def collect_repo_resource_snapshot(
    *, path: str | Path = "/", repo_root: str | Path
) -> ResourceSnapshot:
    return collect_resource_snapshot(path=path, repo_root=repo_root)


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
    values = [sample.get(key) for sample in samples if isinstance(sample.get(key), int | float)]
    return round(sum(values) / len(values), 3) if values else None


def _max(samples: list[dict[str, Any]], key: str) -> float | None:
    values = [sample.get(key) for sample in samples if isinstance(sample.get(key), int | float)]
    return max(values, default=None)


def _latest_numeric(samples: list[dict[str, Any]], key: str) -> int | float | None:
    for sample in reversed(samples):
        value = sample.get(key)
        if isinstance(value, int | float):
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
        "latest_sample_at": samples[-1].get("timestamp") if samples else None,
    }


def write_summary(summary_path: str | Path, summary: dict[str, Any]) -> None:
    summary_path = Path(summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def summarize_delta_pairs(profile_path: str | Path) -> dict[str, Any]:
    """Compute resource deltas from matched pre/post sample pairs.

    Pairs are matched sequentially per name: each 'post' sample consumes the
    most recent 'pre' sample with the same name.  Snapshot-only profiles that
    lack pre/post pairs return ``{"pair_count": 0}``.
    """
    profile_path = Path(profile_path)
    if not profile_path.exists():
        return {"pair_count": 0}

    data = _load_profile_document(profile_path)
    samples = data.get("samples", [])

    pending_pre: dict[str, dict[str, Any]] = {}
    deltas: list[dict[str, Any]] = []
    for sample in samples:
        name = sample.get("name", "")
        phase = sample.get("phase", "")
        if phase == "pre":
            pending_pre[name] = sample
        elif phase == "post" and name in pending_pre:
            pre = pending_pre.pop(name)
            delta: dict[str, Any] = {}
            for key in ("cpu_percent", "memory_used_bytes", "gpu_util_percent"):
                pv, sv = pre.get(key), sample.get(key)
                if isinstance(pv, int | float) and isinstance(sv, int | float):
                    delta[key] = sv - pv
            if delta:
                deltas.append(delta)

    if not deltas:
        return {"pair_count": 0}

    def _avg_delta(key: str) -> float | None:
        vals = [d[key] for d in deltas if isinstance(d.get(key), int | float)]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "pair_count": len(deltas),
        "avg_delta_cpu_percent": _avg_delta("cpu_percent"),
        "avg_delta_memory_used_bytes": _avg_delta("memory_used_bytes"),
        "avg_delta_gpu_util_percent": _avg_delta("gpu_util_percent"),
    }


_RUN_WINDOW = 10  # number of most-recent qualifying runs to aggregate


def summarize_run_records(profile_path: str | Path) -> dict[str, Any]:
    """Aggregate per-process metrics from the most recent qualifying run records.

    Only the last ``_RUN_WINDOW`` runs that contain psutil data are used so
    that stale or pre-normalisation records age out naturally.
    """
    profile_path = Path(profile_path)
    if not profile_path.exists():
        return {"run_count": 0, "qualifying_run_count": 0}

    data = _load_profile_document(profile_path)
    runs = data.get("runs", [])
    qualifying = [r for r in runs if "proc_avg_cpu_percent" in r][-_RUN_WINDOW:]

    if not qualifying:
        return {"run_count": len(runs), "qualifying_run_count": 0}

    def _qa(key: str) -> float | None:
        vals = [r[key] for r in qualifying if isinstance(r.get(key), int | float)]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "run_count": len(runs),
        "qualifying_run_count": len(qualifying),
        "avg_proc_cpu_percent": _qa("proc_avg_cpu_percent"),
        "avg_proc_peak_cpu_percent": _qa("proc_peak_cpu_percent"),
        "avg_proc_memory_rss_bytes": _qa("proc_avg_memory_rss_bytes"),
        "avg_proc_peak_memory_rss_bytes": _qa("proc_peak_memory_rss_bytes"),
    }


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
    # rusage snapshot before spawn — delta gives exact CPU time of the child tree.
    _rusage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    _proc_samples: list[dict[str, float]] = []
    _stop_event = threading.Event()
    _monitor_thread: threading.Thread | None = None
    if _PSUTIL_AVAILABLE:
        _monitor_thread = threading.Thread(
            target=_monitor_process,
            args=(proc.pid, _proc_samples, _stop_event),
            daemon=True,
        )
        _monitor_thread.start()

    stdout, stderr = proc.communicate()
    runtime = round(time.time() - started, 3)
    _stop_event.set()
    if _monitor_thread is not None:
        _monitor_thread.join(timeout=2.0)

    # rusage after — captures CPU time even for processes too fast to psutil-sample.
    _rusage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    _cpu_time = (_rusage_after.ru_utime - _rusage_before.ru_utime) + (
        _rusage_after.ru_stime - _rusage_before.ru_stime
    )
    # ru_maxrss is in kilobytes on Linux.
    _rusage_peak_rss = int(_rusage_after.ru_maxrss) * 1024

    if _proc_samples:
        cpu_vals = [s["cpu_percent"] for s in _proc_samples]
        rss_vals = [s["memory_rss_bytes"] for s in _proc_samples]
        proc_metrics: dict[str, Any] = {
            "proc_avg_cpu_percent": round(sum(cpu_vals) / len(cpu_vals), 3),
            "proc_peak_cpu_percent": round(max(cpu_vals), 3),
            "proc_avg_memory_rss_bytes": int(sum(rss_vals) / len(rss_vals)),
            "proc_peak_memory_rss_bytes": int(max(rss_vals)),
            "proc_sample_count": len(_proc_samples),
        }
    else:
        # Fallback: derive avg CPU% from rusage wall-clock accounting,
        # normalised by cpu_count to match the psutil path (0–100 % of total capacity).
        _cpu_count = os.cpu_count() or 1
        _avg_cpu = round(_cpu_time / runtime * 100 / _cpu_count, 3) if runtime > 0 else 0.0
        proc_metrics = {
            "proc_avg_cpu_percent": _avg_cpu,
            "proc_peak_cpu_percent": _avg_cpu,
            "proc_avg_memory_rss_bytes": _rusage_peak_rss,
            "proc_peak_memory_rss_bytes": _rusage_peak_rss,
            "proc_sample_count": 0,
        }

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
        "stdout_tail": stdout[-capture_output_bytes:] if capture_output_bytes else "",
        "stderr_tail": stderr[-capture_output_bytes:] if capture_output_bytes else "",
        "summary": summary,
        **proc_metrics,
    }
    append_run_record(profile_path, record, repo_metadata=repo_metadata)
    return record
