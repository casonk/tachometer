"""Per-repo red-light backlog tracking.

When a stoplight metric hits red during a snapshot or run, an entry is
written to ``.tachometer/backlog.json`` inside the repo.  Repeated red
readings increment the occurrence counter rather than duplicating entries.
Entries are auto-resolved when the same metric is no longer red on the next
check.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# Human-readable suggestions keyed by the light name used in stoplight results.
_SUGGESTIONS: dict[str, list[str]] = {
    # system view — load average
    "loadavg": [
        "High sustained load; reduce background tasks or schedule CPU-heavy work during off-peak hours.",
        "Or adjust the loadavg_ratio threshold.",
    ],
    "swap": [
        "System is using swap; reduce memory pressure by closing unused processes or increasing RAM.",
        "Or adjust the swap_utilization_ratio threshold.",
    ],
    "gpu_mem": [
        "GPU VRAM is filling up; reduce batch sizes or free unused GPU tensors.",
        "Or adjust the gpu_mem_utilization_ratio threshold.",
    ],
    # system view
    "cpu": [
        "Reduce system-wide CPU load; schedule heavy tasks during off-peak hours.",
        "Or adjust the cpu_percent threshold if this utilisation level is acceptable.",
    ],
    "memory": [
        "Free system memory by closing unused processes or increasing swap.",
        "Or adjust the memory_utilization_ratio threshold.",
    ],
    "disk": [
        "Clean up disk space, remove build artefacts, or expand storage.",
        "Or adjust the disk_utilization_ratio threshold.",
    ],
    "gpu": [
        "Reduce GPU workload; schedule GPU-intensive tasks during off-peak hours.",
        "Or adjust the gpu_util_percent threshold.",
    ],
    "repo_size": [
        "Remove large binaries, update .gitignore, or migrate to git-lfs.",
        "Or adjust the repo_size_bytes threshold.",
    ],
    # delta view
    "delta_cpu": [
        "Optimise the profiled command's CPU usage; consider reducing parallelism.",
        "Or adjust the delta_cpu_percent threshold.",
    ],
    "delta_memory": [
        "Reduce memory allocations in the profiled command.",
        "Or adjust the delta_memory_used_bytes threshold.",
    ],
    "delta_gpu": [
        "Reduce GPU utilisation during the profiled command.",
        "Or adjust the delta_gpu_util_percent threshold.",
    ],
    "delta_disk_read": [
        "Reduce file reads in the profiled command; consider caching or memoising expensive I/O.",
        "Or adjust the delta_disk_io_read_bytes threshold.",
    ],
    "delta_disk_write": [
        "Reduce file writes; avoid writing large temporary files or redundant build artefacts.",
        "Or adjust the delta_disk_io_write_bytes threshold.",
    ],
    "delta_net_recv": [
        "Reduce network downloads during the run; cache remote fixtures or mock external calls.",
        "Or adjust the delta_net_recv_bytes threshold.",
    ],
    "delta_net_sent": [
        "Reduce outbound network traffic during the run; mock external API calls in tests.",
        "Or adjust the delta_net_sent_bytes threshold.",
    ],
    # process view
    "proc_threads": [
        "Peak thread count is high; check for thread leaks or excessive parallelism.",
        "Or adjust the proc_peak_thread_count threshold.",
    ],
    "proc_major_faults": [
        "Many major page faults indicate memory pressure and disk-backed paging; reduce working set size.",
        "Or adjust the proc_major_faults threshold.",
    ],
    "artefact_size": [
        "Delete or .gitignore build artefacts (dist/, build/, *.egg-info/); run `python -m build` only when needed.",
        "Or adjust the artefact_size_bytes threshold.",
    ],
    "proc_invol_ctx": [
        "High involuntary context switches indicate CPU contention; reduce parallelism or competing processes.",
        "Or adjust the proc_involuntary_ctx_switches threshold.",
    ],
    "proc_runtime": [
        "Profile the command to find bottlenecks; consider parallelism or caching slow steps.",
        "Or adjust the proc_avg_runtime_seconds threshold.",
    ],
    "proc_avg_cpu": [
        "Profile and optimise CPU-intensive code paths in the command.",
        "Or adjust the proc_avg_cpu_percent threshold.",
    ],
    "proc_peak_cpu": [
        "Reduce peak CPU spikes; look for tight loops or unthrottled parallelism.",
        "Or adjust the proc_peak_cpu_percent threshold.",
    ],
    "proc_avg_rss": [
        "Reduce memory allocations; profile with memory-profiler or tracemalloc.",
        "Or adjust the proc_avg_memory_rss_bytes threshold.",
    ],
    "proc_peak_rss": [
        "Reduce peak memory usage; check for large temporary allocations or leaks.",
        "Or adjust the proc_peak_memory_rss_bytes threshold.",
    ],
}

_FALLBACK_SUGGESTIONS = [
    "Investigate and reduce this resource's utilisation.",
    "Or adjust the threshold if this level is acceptable for this repo.",
]


def _entry_id(view: str, light_key: str) -> str:
    return f"{view}.{light_key}"


def load_backlog(backlog_path: Path) -> list[dict[str, Any]]:
    if not backlog_path.exists():
        return []
    try:
        return json.loads(backlog_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_backlog(backlog_path: Path, entries: list[dict[str, Any]]) -> None:
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def update_backlog(
    backlog_path: Path,
    view: str,
    stoplight_result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create, update, or auto-resolve backlog entries from a stoplight result.

    - Red light, entry absent or dismissed → create new open entry.
    - Red light, entry already open → bump occurrence_count + last_detected_at.
    - Non-red light, entry open → mark as auto-resolved.

    Returns ``(updated_entries, newly_opened_entries)``.
    """
    entries = load_backlog(backlog_path)
    by_id: dict[str, dict[str, Any]] = {e["id"]: e for e in entries}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")

    lights = stoplight_result.get("lights", {})
    metrics = stoplight_result.get("metrics", {})

    # Build a metric-value lookup from the lights → metrics name mapping.
    # Both dicts come from the same stoplight evaluate call so we can zip them.
    light_to_metric_value: dict[str, Any] = {}
    for light_key in lights:
        # Try direct match first, then common suffixed variants.
        for candidate in (light_key, f"{light_key}_percent", f"avg_{light_key}_percent"):
            if candidate in metrics:
                light_to_metric_value[light_key] = metrics[candidate]
                break
        # Fallback: scan metrics for a key that contains the light key.
        if light_key not in light_to_metric_value:
            for mk, mv in metrics.items():
                if light_key.replace("_", "") in mk.replace("_", ""):
                    light_to_metric_value[light_key] = mv
                    break

    newly_opened: list[dict[str, Any]] = []
    for light_key, light in lights.items():
        entry_id = _entry_id(view, light_key)
        value = light_to_metric_value.get(light_key)

        if light == "red":
            existing = by_id.get(entry_id)
            if existing and existing.get("status") == "open":
                existing["last_detected_at"] = now_iso
                existing["occurrence_count"] = existing.get("occurrence_count", 1) + 1
                if value is not None:
                    existing["value"] = value
            else:
                new_entry: dict[str, Any] = {
                    "id": entry_id,
                    "view": view,
                    "light_key": light_key,
                    "value": value,
                    "first_detected_at": now_iso,
                    "last_detected_at": now_iso,
                    "occurrence_count": 1,
                    "status": "open",
                    "suggestions": _SUGGESTIONS.get(light_key, _FALLBACK_SUGGESTIONS),
                }
                by_id[entry_id] = new_entry
                newly_opened.append(new_entry)
        elif light in ("green", "yellow"):
            existing = by_id.get(entry_id)
            if existing and existing.get("status") == "open":
                existing["status"] = "auto-resolved"
                existing["resolved_at"] = now_iso

    updated = list(by_id.values())
    save_backlog(backlog_path, updated)
    return updated, newly_opened


def open_items(backlog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the entries that are currently open."""
    return [e for e in backlog if e.get("status") == "open"]
