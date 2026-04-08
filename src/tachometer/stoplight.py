"""Stoplight signal evaluation for tachometer resource summaries.

Thresholds are aligned with doseido's DEFAULT_TARGETS so that the same
device-utilisation conventions apply portfolio-wide.  Downstream repos may
supply their own thresholds dict to override.
"""
from __future__ import annotations

from typing import Any

DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu_percent": {"green_max": 60.0, "yellow_max": 85.0},
    "memory_utilization_ratio": {"green_max": 0.70, "yellow_max": 0.85},
    "disk_utilization_ratio": {"green_max": 0.75, "yellow_max": 0.90},
    "gpu_util_percent": {"green_max": 50.0, "yellow_max": 80.0},
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

    mem_ratio = (mem_used / mem_total) if (mem_used is not None and mem_total) else None
    disk_ratio = (disk_used / disk_total) if (disk_used is not None and disk_total) else None

    lights = {
        "cpu": light_max(cpu, **t["cpu_percent"]),
        "memory": light_max(mem_ratio, **t["memory_utilization_ratio"]),
        "disk": light_max(disk_ratio, **t["disk_utilization_ratio"]),
        "gpu": light_max(gpu, **t["gpu_util_percent"]),
    }
    overall = worst_light(lights)

    return {
        "metrics": {
            "cpu_percent": cpu,
            "memory_utilization_ratio": mem_ratio,
            "disk_utilization_ratio": disk_ratio,
            "gpu_util_percent": gpu,
        },
        "lights": lights,
        "overall_light": overall,
        "backoff_action": backoff_action(overall),
    }
