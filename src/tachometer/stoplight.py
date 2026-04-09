"""Stoplight signal evaluation for tachometer resource summaries.

Thresholds are aligned with doseido's DEFAULT_TARGETS so that the same
device-utilisation conventions apply portfolio-wide.  Downstream repos may
supply their own thresholds dict to override.
"""
from __future__ import annotations

from typing import Any

DELTA_THRESHOLDS: dict[str, dict[str, float]] = {
    # How much system resource usage may increase during a run before flagging.
    "delta_cpu_percent":       {"green_max": 40.0, "yellow_max": 70.0},
    "delta_memory_used_bytes": {"green_max": 500e6, "yellow_max": 2e9},
    "delta_gpu_util_percent":  {"green_max": 30.0, "yellow_max": 60.0},
}

PROCESS_THRESHOLDS: dict[str, dict[str, float]] = {
    # Per-process-tree resource consumption via psutil.
    "proc_avg_cpu_percent":           {"green_max": 50.0, "yellow_max": 80.0},
    "proc_peak_cpu_percent":          {"green_max": 80.0, "yellow_max": 95.0},
    "proc_avg_memory_rss_bytes":      {"green_max": 500e6, "yellow_max": 2e9},
    "proc_peak_memory_rss_bytes":     {"green_max": 1e9,   "yellow_max": 4e9},
}

DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu_percent": {"green_max": 60.0, "yellow_max": 85.0},
    "memory_utilization_ratio": {"green_max": 0.70, "yellow_max": 0.85},
    "disk_utilization_ratio": {"green_max": 0.75, "yellow_max": 0.90},
    "gpu_util_percent": {"green_max": 50.0, "yellow_max": 80.0},
    # Repo size: green < 1 GB, yellow 1–10 GB, red > 10 GB
    "repo_size_bytes": {"green_max": 1e9, "yellow_max": 10e9},
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

    cpu   = delta_summary.get("avg_delta_cpu_percent")
    mem   = delta_summary.get("avg_delta_memory_used_bytes")
    gpu   = delta_summary.get("avg_delta_gpu_util_percent")

    # Negative deltas mean the system got lighter — always green.
    cpu_clamped = max(0.0, cpu) if cpu is not None else None
    mem_clamped = max(0.0, mem) if mem is not None else None
    gpu_clamped = max(0.0, gpu) if gpu is not None else None

    lights = {
        "delta_cpu":    light_max(cpu_clamped,  **t["delta_cpu_percent"]),
        "delta_memory": light_max(mem_clamped,  **t["delta_memory_used_bytes"]),
        "delta_gpu":    light_max(gpu_clamped,  **t["delta_gpu_util_percent"]),
    }
    overall = worst_light(lights)
    return {
        "metrics": {
            "avg_delta_cpu_percent":       cpu,
            "avg_delta_memory_used_bytes": mem,
            "avg_delta_gpu_util_percent":  gpu,
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

    avg_cpu  = run_summary.get("avg_proc_cpu_percent")
    peak_cpu = run_summary.get("avg_proc_peak_cpu_percent")
    avg_rss  = run_summary.get("avg_proc_memory_rss_bytes")
    peak_rss = run_summary.get("avg_proc_peak_memory_rss_bytes")

    lights = {
        "proc_avg_cpu":   light_max(avg_cpu,  **t["proc_avg_cpu_percent"]),
        "proc_peak_cpu":  light_max(peak_cpu, **t["proc_peak_cpu_percent"]),
        "proc_avg_rss":   light_max(avg_rss,  **t["proc_avg_memory_rss_bytes"]),
        "proc_peak_rss":  light_max(peak_rss, **t["proc_peak_memory_rss_bytes"]),
    }
    overall = worst_light(lights)
    return {
        "metrics": {
            "avg_proc_cpu_percent":          avg_cpu,
            "avg_proc_peak_cpu_percent":     peak_cpu,
            "avg_proc_memory_rss_bytes":     avg_rss,
            "avg_proc_peak_memory_rss_bytes": peak_rss,
        },
        "lights": lights,
        "overall_light": overall,
        "backoff_action": backoff_action(overall),
    }


def evaluate(
    summary: dict[str, Any],
    thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Compute stoplight signals from a tachometer summary dict.

    Expects the keys produced by ``summarize_samples``, including the
    ``latest_memory_total_bytes`` and ``latest_disk_total_bytes`` fields
    added to carry hardware totals for ratio computation.
    """
    t = thresholds or DEFAULT_THRESHOLDS

    cpu = summary.get("avg_cpu_percent")
    mem_used = summary.get("avg_memory_used_bytes")
    mem_total = summary.get("latest_memory_total_bytes")
    disk_used = summary.get("avg_disk_used_bytes")
    disk_total = summary.get("latest_disk_total_bytes")
    gpu = summary.get("avg_gpu_util_percent")
    repo_size = summary.get("latest_repo_size_bytes")

    mem_ratio = (mem_used / mem_total) if (mem_used is not None and mem_total) else None
    disk_ratio = (disk_used / disk_total) if (disk_used is not None and disk_total) else None

    lights = {
        "cpu": light_max(cpu, **t["cpu_percent"]),
        "memory": light_max(mem_ratio, **t["memory_utilization_ratio"]),
        "disk": light_max(disk_ratio, **t["disk_utilization_ratio"]),
        "gpu": light_max(gpu, **t["gpu_util_percent"]),
        "repo_size": light_max(repo_size, **t["repo_size_bytes"]),
    }
    overall = worst_light(lights)

    return {
        "metrics": {
            "cpu_percent": cpu,
            "memory_utilization_ratio": mem_ratio,
            "disk_utilization_ratio": disk_ratio,
            "gpu_util_percent": gpu,
            "repo_size_bytes": repo_size,
        },
        "lights": lights,
        "overall_light": overall,
        "backoff_action": backoff_action(overall),
    }
