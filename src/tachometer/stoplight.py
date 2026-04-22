"""Stoplight signal evaluation for tachometer resource summaries.

Thresholds are aligned with doseido's DEFAULT_TARGETS so that the same
device-utilisation conventions apply portfolio-wide.  Downstream repos may
supply their own thresholds dict to override.
"""

from __future__ import annotations

from typing import Any

DELTA_THRESHOLDS: dict[str, dict[str, float]] = {
    # How much system resource usage may increase during a run before flagging.
    "delta_cpu_percent": {"green_max": 40.0, "yellow_max": 70.0},
    "delta_memory_used_bytes": {"green_max": 500e6, "yellow_max": 2e9},
    "delta_gpu_util_percent": {"green_max": 30.0, "yellow_max": 60.0},
    # Disk I/O during the run (cumulative bytes transferred).
    "delta_disk_io_read_bytes": {"green_max": 500e6, "yellow_max": 5e9},
    "delta_disk_io_write_bytes": {"green_max": 200e6, "yellow_max": 2e9},
    # Network I/O during the run.
    "delta_net_recv_bytes": {"green_max": 50e6, "yellow_max": 500e6},
    "delta_net_sent_bytes": {"green_max": 50e6, "yellow_max": 500e6},
}

PROCESS_THRESHOLDS: dict[str, dict[str, float]] = {
    # Per-process-tree resource consumption via psutil.
    "proc_avg_cpu_percent": {"green_max": 50.0, "yellow_max": 80.0},
    "proc_peak_cpu_percent": {"green_max": 80.0, "yellow_max": 95.0},
    "proc_avg_memory_rss_bytes": {"green_max": 500e6, "yellow_max": 2e9},
    "proc_peak_memory_rss_bytes": {"green_max": 1e9, "yellow_max": 4e9},
    # Wall-clock duration of the run command.
    "proc_avg_runtime_seconds": {"green_max": 60.0, "yellow_max": 300.0},
    # Thread, fault, and context switch counts.
    "proc_peak_thread_count": {"green_max": 100.0, "yellow_max": 500.0},
    "proc_major_faults": {"green_max": 500.0, "yellow_max": 5000.0},
    "proc_involuntary_ctx_switches": {"green_max": 10_000.0, "yellow_max": 100_000.0},
}

DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu_percent": {"green_max": 60.0, "yellow_max": 85.0},
    "loadavg_ratio": {"green_max": 0.70, "yellow_max": 0.90},
    "memory_utilization_ratio": {"green_max": 0.70, "yellow_max": 0.85},
    "swap_utilization_ratio": {"green_max": 0.10, "yellow_max": 0.40},
    "disk_utilization_ratio": {"green_max": 0.75, "yellow_max": 0.90},
    "gpu_util_percent": {"green_max": 50.0, "yellow_max": 80.0},
    "gpu_mem_utilization_ratio": {"green_max": 0.70, "yellow_max": 0.90},
    # Repo size: green < 1 GB, yellow 1–10 GB, red > 10 GB
    "repo_size_bytes": {"green_max": 1e9, "yellow_max": 10e9},
    # Build artefacts: green < 100 MB, yellow < 1 GB
    "artefact_size_bytes": {"green_max": 100e6, "yellow_max": 1e9},
}

HOST_THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu_percent": {"green_max": 60.0, "yellow_max": 85.0},
    "loadavg_ratio": {"green_max": 0.70, "yellow_max": 0.90},
    "memory_utilization_ratio": {"green_max": 0.70, "yellow_max": 0.85},
    "swap_utilization_ratio": {"green_max": 0.10, "yellow_max": 0.40},
    "disk_utilization_ratio": {"green_max": 0.75, "yellow_max": 0.90},
    "gpu_util_percent": {"green_max": 50.0, "yellow_max": 80.0},
    "gpu_mem_utilization_ratio": {"green_max": 0.70, "yellow_max": 0.90},
}


def light_max(value: float | None, green_max: float, yellow_max: float) -> str:
    """Return green/yellow/red/unknown based on a max-is-bad metric."""
    if value is None:
        return "unknown"
    if value <= green_max:
        return "green"
    if value <= yellow_max:
        return "yellow"
    return "red"


def worst_light(lights: dict[str, str]) -> str:
    """Return the most severe light across a dict of named lights."""
    priority = {"red": 3, "yellow": 2, "unknown": 1, "green": 0}
    worst = "green"
    for light in lights.values():
        if priority.get(light, 0) > priority.get(worst, 0):
            worst = light
    return worst


def backoff_action(light: str) -> str:
    """Translate a stoplight colour to a recommended scheduler action."""
    return {
        "green": "no_backoff",
        "yellow": "soft_backoff",
        "red": "hard_backoff",
    }.get(light, "observe")


def evaluate_delta(
    delta_summary: dict[str, Any],
    thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Evaluate stoplight signals from delta (pre→post) resource changes."""
    t = thresholds or DELTA_THRESHOLDS

    cpu = delta_summary.get("avg_delta_cpu_percent")
    mem = delta_summary.get("avg_delta_memory_used_bytes")
    gpu = delta_summary.get("avg_delta_gpu_util_percent")
    disk_read = delta_summary.get("avg_delta_disk_io_read_bytes")
    disk_write = delta_summary.get("avg_delta_disk_io_write_bytes")
    net_recv = delta_summary.get("avg_delta_net_recv_bytes")
    net_sent = delta_summary.get("avg_delta_net_sent_bytes")

    def _clamp(v: float | None) -> float | None:
        return max(0.0, v) if v is not None else None

    lights = {
        "delta_cpu": light_max(_clamp(cpu), **t["delta_cpu_percent"]),
        "delta_memory": light_max(_clamp(mem), **t["delta_memory_used_bytes"]),
        "delta_gpu": light_max(_clamp(gpu), **t["delta_gpu_util_percent"]),
        "delta_disk_read": light_max(_clamp(disk_read), **t["delta_disk_io_read_bytes"]),
        "delta_disk_write": light_max(_clamp(disk_write), **t["delta_disk_io_write_bytes"]),
        "delta_net_recv": light_max(_clamp(net_recv), **t["delta_net_recv_bytes"]),
        "delta_net_sent": light_max(_clamp(net_sent), **t["delta_net_sent_bytes"]),
    }
    overall = worst_light(lights)
    return {
        "metrics": {
            "avg_delta_cpu_percent": cpu,
            "avg_delta_memory_used_bytes": mem,
            "avg_delta_gpu_util_percent": gpu,
            "avg_delta_disk_io_read_bytes": disk_read,
            "avg_delta_disk_io_write_bytes": disk_write,
            "avg_delta_net_recv_bytes": net_recv,
            "avg_delta_net_sent_bytes": net_sent,
        },
        "lights": lights,
        "overall_light": overall,
        "backoff_action": backoff_action(overall),
    }


def evaluate_process(
    run_summary: dict[str, Any],
    thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Evaluate stoplight signals from per-process psutil metrics."""
    t = thresholds or PROCESS_THRESHOLDS

    avg_cpu = run_summary.get("avg_proc_cpu_percent")
    peak_cpu = run_summary.get("avg_proc_peak_cpu_percent")
    avg_rss = run_summary.get("avg_proc_memory_rss_bytes")
    peak_rss = run_summary.get("avg_proc_peak_memory_rss_bytes")
    avg_runtime = run_summary.get("avg_runtime_seconds")
    peak_threads = run_summary.get("avg_proc_peak_thread_count")
    major_faults = run_summary.get("avg_proc_major_faults")
    invol_ctx = run_summary.get("avg_proc_involuntary_ctx_switches")

    lights = {
        "proc_avg_cpu": light_max(avg_cpu, **t["proc_avg_cpu_percent"]),
        "proc_peak_cpu": light_max(peak_cpu, **t["proc_peak_cpu_percent"]),
        "proc_avg_rss": light_max(avg_rss, **t["proc_avg_memory_rss_bytes"]),
        "proc_peak_rss": light_max(peak_rss, **t["proc_peak_memory_rss_bytes"]),
        "proc_runtime": light_max(avg_runtime, **t["proc_avg_runtime_seconds"]),
        "proc_threads": light_max(peak_threads, **t["proc_peak_thread_count"]),
        "proc_major_faults": light_max(major_faults, **t["proc_major_faults"]),
        "proc_invol_ctx": light_max(invol_ctx, **t["proc_involuntary_ctx_switches"]),
    }
    overall = worst_light(lights)
    return {
        "metrics": {
            "avg_proc_cpu_percent": avg_cpu,
            "avg_proc_peak_cpu_percent": peak_cpu,
            "avg_proc_memory_rss_bytes": avg_rss,
            "avg_proc_peak_memory_rss_bytes": peak_rss,
            "avg_runtime_seconds": avg_runtime,
            "avg_proc_peak_thread_count": peak_threads,
            "avg_proc_major_faults": major_faults,
            "avg_proc_involuntary_ctx_switches": invol_ctx,
        },
        "lights": lights,
        "overall_light": overall,
        "backoff_action": backoff_action(overall),
    }


def evaluate(
    summary: dict[str, Any],
    thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Compute repo stoplight signals from a tachometer summary dict."""
    return _evaluate_summary(
        summary,
        thresholds or DEFAULT_THRESHOLDS,
        include_repo_size=True,
    )


def evaluate_host(
    summary: dict[str, Any],
    thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Compute canonical host stoplight signals from a tachometer summary dict."""
    return _evaluate_summary(
        summary,
        thresholds or HOST_THRESHOLDS,
        include_repo_size=False,
    )


def _evaluate_summary(
    summary: dict[str, Any],
    thresholds: dict[str, dict[str, float]],
    *,
    include_repo_size: bool,
) -> dict[str, Any]:
    """Compute stoplight signals from a tachometer summary dict."""
    t = thresholds
    cpu = summary.get("avg_cpu_percent")
    load1 = summary.get("avg_loadavg_1m")
    cpu_count = summary.get("latest_cpu_count") or 1
    mem_used = summary.get("avg_memory_used_bytes")
    mem_total = summary.get("latest_memory_total_bytes")
    swap_used = summary.get("avg_swap_used_bytes")
    swap_total = summary.get("latest_swap_total_bytes")
    disk_used = summary.get("avg_disk_used_bytes")
    disk_total = summary.get("latest_disk_total_bytes")
    gpu = summary.get("avg_gpu_util_percent")
    gpu_mem_used = summary.get("avg_gpu_mem_used_mb")
    gpu_mem_total = summary.get("latest_gpu_mem_total_mb")
    repo_size = summary.get("latest_repo_size_bytes")
    artefact_size = summary.get("latest_artefact_size_bytes")

    mem_ratio = (mem_used / mem_total) if (mem_used is not None and mem_total) else None
    swap_ratio = (swap_used / swap_total) if (swap_used is not None and swap_total) else None
    disk_ratio = (disk_used / disk_total) if (disk_used is not None and disk_total) else None
    gpu_mem_ratio = (
        (gpu_mem_used / gpu_mem_total) if (gpu_mem_used is not None and gpu_mem_total) else None
    )
    load_ratio = (load1 / cpu_count) if load1 is not None else None

    lights = {
        "cpu": light_max(cpu, **t["cpu_percent"]),
        "loadavg": light_max(load_ratio, **t["loadavg_ratio"]),
        "memory": light_max(mem_ratio, **t["memory_utilization_ratio"]),
        "swap": light_max(swap_ratio, **t["swap_utilization_ratio"]),
        "disk": light_max(disk_ratio, **t["disk_utilization_ratio"]),
        "gpu": light_max(gpu, **t["gpu_util_percent"]),
        "gpu_mem": light_max(gpu_mem_ratio, **t["gpu_mem_utilization_ratio"]),
    }
    if include_repo_size:
        lights["repo_size"] = light_max(repo_size, **t["repo_size_bytes"])
        lights["artefact_size"] = light_max(artefact_size, **t["artefact_size_bytes"])
    overall = worst_light(lights)

    metrics = {
        "cpu_percent": cpu,
        "loadavg_ratio": load_ratio,
        "memory_utilization_ratio": mem_ratio,
        "swap_utilization_ratio": swap_ratio,
        "disk_utilization_ratio": disk_ratio,
        "gpu_util_percent": gpu,
        "gpu_mem_utilization_ratio": gpu_mem_ratio,
    }
    if include_repo_size:
        metrics["repo_size_bytes"] = repo_size
        metrics["artefact_size_bytes"] = artefact_size

    return {
        "metrics": metrics,
        "lights": lights,
        "overall_light": overall,
        "backoff_action": backoff_action(overall),
    }
