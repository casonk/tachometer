"""Portfolio-wide stoplight dashboard server.

Serves a human-readable gauge page at / and a machine-readable JSON status
feed at /api/status.  Red-light repos are flagged for throttle / hard-backoff
so that consuming services can self-regulate.

Three metric views are available via ?view=system|delta|process:
  system  — system-wide snapshots (CPU/mem/GPU from /proc & nvidia-smi)
  delta   — how much system resources changed pre→post during run subcommands
  process — actual per-process-tree resource use via psutil during run

Usage (via CLI):
    tachometer serve --manifest config/tachometer/profile.toml --port 5100
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from .backlog import load_backlog, open_items
from .profile import summarize_delta_pairs, summarize_run_records
from .stoplight import (
    DEFAULT_THRESHOLDS,
    DELTA_THRESHOLDS,
    PROCESS_THRESHOLDS,
    backoff_action,
    evaluate_delta,
    evaluate_host,
    evaluate_process,
    light_max,
    worst_light,
)
from .stoplight import evaluate as stoplight_evaluate

# ---------------------------------------------------------------------------
# Background snapshot runner
# ---------------------------------------------------------------------------

_RUN_LOCK = threading.Lock()
_RUN_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_exit": None,
    "last_finished": None,
    "log_path": None,
}


def _is_loopback_host(host: str) -> bool:
    candidate = host.strip()
    if not candidate:
        return False
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _validate_bind_host(host: str, *, allow_remote: bool) -> None:
    if _is_loopback_host(host) or allow_remote:
        return
    raise ValueError(
        "Refusing to bind tachometer serve to a non-loopback host without --allow-remote."
    )


def _same_origin_request(headers: Any) -> bool:
    host = str(headers.get("Host", "")).strip()
    if not host:
        return False
    source = str(headers.get("Origin") or headers.get("Referer") or "").strip()
    if not source:
        return False
    parsed = urlparse(source)
    return bool(parsed.scheme and parsed.netloc and parsed.netloc == host)


def _start_snapshot_run(tachometer_root: Path) -> bool:
    """Launch run_all_tachometer_snapshots.sh in the background.

    Returns True if the run was started, False if one is already in progress.
    stdout+stderr are written to .tachometer/run-all.log for debugging.
    """
    with _RUN_LOCK:
        if _RUN_STATE["running"]:
            return False
        _RUN_STATE["running"] = True
        _RUN_STATE["started_at"] = time.time()
        _RUN_STATE["last_exit"] = None

    log_path = tachometer_root / ".tachometer" / "run-all.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _RUN_STATE["log_path"] = log_path
    script = tachometer_root / "scripts" / "run_all_tachometer_snapshots.sh"

    def _run() -> None:
        with open(log_path, "w", encoding="utf-8") as log_f:
            proc = subprocess.Popen(
                ["bash", str(script)],
                cwd=str(tachometer_root),
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            proc.wait()
        with _RUN_LOCK:
            _RUN_STATE["running"] = False
            _RUN_STATE["last_exit"] = proc.returncode
            _RUN_STATE["last_finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_downstream_repos(tachometer_root: Path) -> list[dict[str, Any]]:
    config_path = tachometer_root / "config" / "downstream-repos.toml"
    if not config_path.exists():
        return []
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return data.get("repos", [])


def _portfolio_root(tachometer_root: Path) -> Path:
    # Standard layout: portfolio/util-repos/tachometer
    return tachometer_root.parent.parent


def _category_from_path(path_str: str) -> str:
    stripped = path_str.lstrip("./")
    return stripped.split("/")[0] if "/" in stripped else stripped


def gather_repo_data(tachometer_root: Path) -> list[dict[str, Any]]:
    portfolio_root = _portfolio_root(tachometer_root)
    repos = _load_downstream_repos(tachometer_root)
    repos.append(
        {
            "name": "tachometer",
            "path": "./util-repos/tachometer",
            "reason": "Self-profile.",
            "run_command": "python3 -m pytest tests/ -q",
        }
    )

    results = []
    for repo in repos:
        path_str = repo.get("path", "")
        repo_dir = portfolio_root / path_str.lstrip("./")
        profile_path = repo_dir / ".tachometer" / "profile.json"
        summary = _load_json(repo_dir / ".tachometer" / "summary.json")
        has_data = bool(summary.get("sample_count", 0))

        # System stoplight — from summary.json averages
        stoplight_system = stoplight_evaluate(summary) if has_data else {}

        # Delta stoplight — from pre/post sample pairs in profile.json
        delta_summary = summarize_delta_pairs(profile_path)
        has_delta = delta_summary.get("pair_count", 0) > 0
        stoplight_delta = evaluate_delta(delta_summary) if has_delta else {}

        # Process stoplight — from psutil run records in profile.json
        run_summary = summarize_run_records(profile_path)
        has_process = run_summary.get("qualifying_run_count", 0) > 0
        stoplight_process = evaluate_process(run_summary) if has_process else {}

        backlog = load_backlog(repo_dir / ".tachometer" / "backlog.json")
        results.append(
            {
                "name": repo["name"],
                "category": _category_from_path(path_str),
                "has_data": has_data,
                "has_delta": has_delta,
                "has_process": has_process,
                "run_command": repo.get("run_command", ""),
                "no_run_reason": repo.get("no_run_reason", ""),
                "summary": summary,
                "delta_summary": delta_summary,
                "run_summary": run_summary,
                "stoplight_system": stoplight_system,
                "stoplight_delta": stoplight_delta,
                "stoplight_process": stoplight_process,
                "backlog": backlog,
                "backlog_open": len(open_items(backlog)),
                "last_run_ts": summary.get("latest_sample_at"),
            }
        )

    return sorted(results, key=lambda r: (r["category"], r["name"]))


def gather_host_data(host_summary_path: Path) -> dict[str, Any]:
    summary = _load_json(host_summary_path)
    has_data = bool(summary.get("sample_count", 0))
    stoplight_host = evaluate_host(summary) if has_data else {}
    host_backlog = load_backlog(host_summary_path.parent / "host-backlog.json")
    return {
        "has_data": has_data,
        "summary": summary,
        "stoplight_host": stoplight_host,
        "backlog": host_backlog,
        "backlog_open": len(open_items(host_backlog)),
    }


def _agent_utilization_sidecar_path(tachometer_root: Path) -> Path:
    return tachometer_root / ".tachometer" / "agent-utilization.json"


def gather_agent_utilization_data(sidecar_path: Path) -> dict[str, Any]:
    snapshot = _load_json(sidecar_path)
    providers = snapshot.get("providers", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(providers, dict):
        providers = {}
    return {
        "has_data": bool(providers),
        "snapshot": snapshot,
        "overall_light": snapshot.get("overall_light", "unknown"),
    }


def _fedora_debug_sidecar_path(tachometer_root: Path) -> Path:
    return (
        _portfolio_root(tachometer_root)
        / "util-repos"
        / "fedora-debugg"
        / "artifacts"
        / "latest"
        / "tachometer-signals.json"
    )


def gather_fedora_debug_data(sidecar_path: Path) -> dict[str, Any]:
    signals = _load_json(sidecar_path)
    has_data = bool(signals)
    return {
        "has_data": has_data,
        "signals": signals,
        "overall_light": signals.get("overall_light", "unknown"),
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_bytes(b: float | None) -> str:
    if b is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _fmt_pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def _fmt_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_age(ts: float | None) -> str:
    """Format a unix timestamp as a human-readable age (e.g. '3h ago')."""
    if ts is None:
        return "—"
    seconds = max(0.0, time.time() - ts)
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d ago"
    if h:
        return f"{h}h ago"
    if m:
        return f"{m}m ago"
    return "just now"


def _fmt_ratio_pct(v: float | None) -> str:
    return _fmt_pct(v * 100 if v is not None else None)


def _fmt_rel_time(seconds: float) -> str:
    """Format a relative time delta (positive = future, negative = past)."""
    future = seconds >= 0
    secs = abs(int(seconds))
    if secs < 60:
        s = f"{secs}s"
    elif secs < 3600:
        m, rem = divmod(secs, 60)
        s = f"{m}m {rem}s" if rem else f"{m}m"
    elif secs < 86400:
        h, rem = divmod(secs, 3600)
        m = rem // 60
        s = f"{h}h {m}m"
    else:
        d, rem = divmod(secs, 86400)
        h = rem // 3600
        s = f"{d}d {h}h"
    return f"in {s}" if future else f"{s} ago"


def _load_schedule_hours(tachometer_root: Path) -> list[int]:
    """Read scheduled hours from portfolio-snapshot.toml; fallback to [0,6,12,18]."""
    config_path = tachometer_root / "config" / "clockwork" / "portfolio-snapshot.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        on_calendar = data.get("on_calendar", "")
        # Format: "*-*-* 0,6,12,18:00:00" — extract the hours part
        if ":" in on_calendar and " " in on_calendar:
            time_part = on_calendar.split(" ")[-1]  # "0,6,12,18:00:00"
            hours_str = time_part.split(":")[0]  # "0,6,12,18"
            return [int(h.strip()) for h in hours_str.split(",")]
    except Exception:
        pass
    return [0, 6, 12, 18]


def _next_schedule_ts(hours: list[int]) -> float:
    """Return the Unix timestamp of the next scheduled run."""
    now = datetime.now()
    for offset in range(2):
        day = now.date() + timedelta(days=offset)
        for h in sorted(hours):
            candidate = datetime(day.year, day.month, day.day, h, 0, 0)
            if candidate > now:
                return candidate.timestamp()
    return (now + timedelta(hours=6)).timestamp()


_LIGHT_COLOR = {
    "green": "#22c55e",
    "yellow": "#eab308",
    "red": "#ef4444",
    "unknown": "#94a3b8",
}

_LIGHT_LABEL = {
    "green": "OK",
    "yellow": "WATCH",
    "red": "THROTTLE",
    "unknown": "—",
}


def _dot(light: str) -> str:
    c = _LIGHT_COLOR.get(light, "#94a3b8")
    return (
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        f'background:{c};vertical-align:middle;margin-right:5px"></span>'
    )


def _badge(light: str) -> str:
    c = _LIGHT_COLOR.get(light, "#94a3b8")
    label = _LIGHT_LABEL.get(light, "—")
    return (
        f'<span style="background:{c};color:white;padding:2px 8px;border-radius:4px;'
        f'font-size:0.7rem;font-weight:700;letter-spacing:.04em">{label}</span>'
    )


def _banner_metric(label: str, value: str, light: str) -> str:
    c = _LIGHT_COLOR.get(light, "#94a3b8")
    return (
        f'<span style="padding:6px 10px;border:1px solid #e2e8f0;border-radius:999px;'
        f'background:#f8fafc;font-size:0.78rem;color:#475569">{label}&nbsp;'
        f'<strong style="color:{c}">{value}</strong></span>'
    )


def _render_host_banner(host: dict[str, Any]) -> str:
    if not host["has_data"]:
        return (
            '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f"{_banner_metric('Host', 'Awaiting snapshot', 'unknown')}"
            "</div>"
        )

    stoplight = host["stoplight_host"]
    lights = stoplight.get("lights", {})
    metrics = stoplight.get("metrics", {})
    backlog_open = host.get("backlog_open", 0)
    host_badge = (
        f'<span title="{backlog_open} open host backlog item(s)" '
        f'style="background:#ef4444;color:white;border-radius:10px;'
        f'padding:1px 6px;font-size:0.65rem;font-weight:700;margin-left:4px"> '
        f"{backlog_open}</span>"
        if backlog_open
        else ""
    )
    summary = host.get("summary", {})
    hostname = summary.get("latest_hostname")
    uptime = summary.get("latest_uptime_seconds")
    procs = summary.get("latest_process_count")
    temp = summary.get("latest_cpu_temp_celsius")

    chips = [
        _banner_metric(
            "Host" + host_badge,
            hostname or _LIGHT_LABEL.get(stoplight.get("overall_light", "unknown"), "—"),
            stoplight.get("overall_light", "unknown"),
        ),
        _banner_metric("Up", _fmt_uptime(uptime), "unknown"),
        _banner_metric(
            "CPU",
            _fmt_pct(metrics.get("cpu_percent")),
            lights.get("cpu", "unknown"),
        ),
        _banner_metric(
            "Memory",
            _fmt_ratio_pct(metrics.get("memory_utilization_ratio")),
            lights.get("memory", "unknown"),
        ),
        _banner_metric(
            "Disk",
            _fmt_ratio_pct(metrics.get("disk_utilization_ratio")),
            lights.get("disk", "unknown"),
        ),
        _banner_metric(
            "GPU",
            _fmt_pct(metrics.get("gpu_util_percent")),
            lights.get("gpu", "unknown"),
        ),
        _banner_metric("Procs", f"{int(procs):,}" if procs is not None else "—", "unknown"),
    ]
    if temp is not None:
        chips.append(_banner_metric("Temp", f"{temp:.0f}°C", "unknown"))

    return (
        '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        + "".join(chips)
        + "</div>"
    )


def _snapshot_age_light(snapshot_epoch: int | float | None) -> str:
    if snapshot_epoch is None:
        return "unknown"
    age_seconds = max(0, time.time() - float(snapshot_epoch))
    if age_seconds <= 6 * 3600:
        return "green"
    if age_seconds <= 24 * 3600:
        return "yellow"
    return "red"


_FEDORA_DEBUG_BUCKET_ORDER = (
    "collection",
    "display",
    "coredumps",
    "gpu",
    "storage",
    "packages",
    "python",
    "node",
    "go",
)


def _legacy_fedora_debug_buckets(signals: dict[str, Any]) -> list[dict[str, str]]:
    lights = signals.get("lights", {})
    metrics = signals.get("metrics", {})
    return [
        {
            "label": "Warnings",
            "summary": str(metrics.get("journal_warning_count", "—")),
            "light": lights.get("warnings", "unknown"),
        },
        {
            "label": "Coredumps",
            "summary": str(metrics.get("current_coredump_marker_count", "—")),
            "light": lights.get("coredumps", "unknown"),
        },
        {
            "label": "GPU",
            "summary": "alert" if metrics.get("gpu_driver_alert") else "ok",
            "light": lights.get("gpu", "unknown"),
        },
    ]


def _fedora_debug_buckets(signals: dict[str, Any]) -> list[dict[str, str]]:
    raw_buckets = signals.get("buckets")
    if not isinstance(raw_buckets, dict):
        return _legacy_fedora_debug_buckets(signals)

    buckets: list[dict[str, str]] = []
    for key in _FEDORA_DEBUG_BUCKET_ORDER:
        bucket = raw_buckets.get(key)
        if not isinstance(bucket, dict):
            continue
        buckets.append(
            {
                "label": str(bucket.get("label", key.title())),
                "summary": str(bucket.get("summary", "—")),
                "light": str(bucket.get("light", "unknown")),
            }
        )

    for key, bucket in raw_buckets.items():
        if key in _FEDORA_DEBUG_BUCKET_ORDER or not isinstance(bucket, dict):
            continue
        buckets.append(
            {
                "label": str(bucket.get("label", key.title())),
                "summary": str(bucket.get("summary", "—")),
                "light": str(bucket.get("light", "unknown")),
            }
        )

    return buckets or _legacy_fedora_debug_buckets(signals)


def _render_fedora_debug_banner(fedora_debug: dict[str, Any]) -> str:
    if not fedora_debug["has_data"]:
        return (
            '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f"{_banner_metric('Fedora Debug', 'Awaiting workflow', 'unknown')}"
            "</div>"
        )

    signals = fedora_debug["signals"]
    snapshot_epoch = signals.get("latest_snapshot_epoch")
    snapshot_light = _snapshot_age_light(snapshot_epoch)
    snapshot_label = (
        _fmt_rel_time(float(snapshot_epoch) - time.time())
        if snapshot_epoch is not None
        else "unknown"
    )
    display_light = worst_light(
        {
            0: fedora_debug.get("overall_light", "unknown"),
            1: snapshot_light,
        }
    )
    chips = [
        _banner_metric(
            "Fedora Debug",
            _LIGHT_LABEL.get(display_light, "—"),
            display_light,
        ),
        _banner_metric(
            "Snapshot",
            snapshot_label,
            snapshot_light,
        ),
    ]
    for bucket in _fedora_debug_buckets(signals):
        chips.append(
            _banner_metric(
                bucket["label"],
                bucket["summary"],
                bucket["light"],
            )
        )
    return (
        '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        + "".join(chips)
        + "</div>"
    )


def _render_agent_utilization_banner(agent_utilization: dict[str, Any]) -> str:
    if not agent_utilization["has_data"]:
        return (
            '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f"{_banner_metric('AI Utilization', 'Awaiting snapshot', 'unknown')}"
            "</div>"
        )

    snapshot = agent_utilization["snapshot"]
    captured_at = snapshot.get("captured_at")
    snapshot_label = (
        _fmt_rel_time(float(captured_at) - time.time())
        if isinstance(captured_at, int | float)
        else "unknown"
    )
    snapshot_light = _snapshot_age_light(captured_at)
    display_light = worst_light(
        {
            0: agent_utilization.get("overall_light", "unknown"),
            1: snapshot_light,
        }
    )
    chips = [
        _banner_metric(
            "AI Utilization",
            _LIGHT_LABEL.get(display_light, "—"),
            display_light,
        ),
        _banner_metric("Snapshot", snapshot_label, snapshot_light),
    ]
    providers = snapshot.get("providers", {})
    if isinstance(providers, dict):
        for key in ("codex", "claude", "copilot"):
            provider = providers.get(key)
            if not isinstance(provider, dict):
                continue
            chips.append(
                _banner_metric(
                    str(provider.get("display_name", key.title())),
                    str(provider.get("summary", "unknown")),
                    str(provider.get("light", "unknown")),
                )
            )
    return (
        '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        + "".join(chips)
        + "</div>"
    )


def _gauge(
    value: float | None,
    max_val: float,
    light: str,
    label: str | None = None,
    green_max: float | None = None,
    yellow_max: float | None = None,
) -> str:
    if value is None:
        return '<span style="color:#94a3b8">—</span>'
    pct = min(100.0, value / max_val * 100.0)
    c = _LIGHT_COLOR.get(light, "#94a3b8")
    display = label if label is not None else f"{value:.1f}"

    markers = ""
    if green_max is not None and max_val > 0:
        gp = min(100.0, green_max / max_val * 100.0)
        markers += (
            f'<div title="yellow threshold: {green_max}" style="position:absolute;top:-1px;'
            f"left:{gp:.1f}%;width:1.5px;height:8px;background:#eab308;"
            f'border-radius:1px;transform:translateX(-50%)"></div>'
        )
    if yellow_max is not None and max_val > 0:
        yp = min(100.0, yellow_max / max_val * 100.0)
        markers += (
            f'<div title="red threshold: {yellow_max}" style="position:absolute;top:-1px;'
            f"left:{yp:.1f}%;width:1.5px;height:8px;background:#ef4444;"
            f'border-radius:1px;transform:translateX(-50%)"></div>'
        )

    return (
        f'<div style="font-size:0.78rem">{display}</div>'
        f'<div style="position:relative;width:80px;height:6px;background:#e2e8f0;'
        f'border-radius:3px;margin-top:2px;overflow:visible">'
        f'<div style="width:{pct:.1f}%;height:6px;background:{c};border-radius:3px"></div>'
        f"{markers}"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_VIEW_LABELS = {
    "system": "System (sys)",
    "delta": "Delta (pre→post)",
    "process": "Process (psutil)",
}

_METRIC_LABELS: dict[str, str] = {
    # system
    "cpu": "CPU",
    "loadavg": "Load",
    "memory": "Memory",
    "swap": "Swap",
    "disk": "Disk",
    "gpu": "GPU",
    "gpu_mem": "VRAM",
    "repo_size": "Repo Size",
    "artefact_size": "Artefacts",
    # delta
    "delta_cpu": "\u0394CPU",
    "delta_memory": "\u0394Mem",
    "delta_gpu": "\u0394GPU",
    "delta_disk_read": "\u0394Disk\u2193",
    "delta_disk_write": "\u0394Disk\u2191",
    "delta_net_recv": "\u0394Net\u2193",
    "delta_net_sent": "\u0394Net\u2191",
    # process
    "proc_avg_cpu": "CPU avg",
    "proc_peak_cpu": "CPU peak",
    "proc_avg_rss": "RSS avg",
    "proc_peak_rss": "RSS peak",
    "proc_runtime": "Runtime",
    "proc_threads": "Threads",
    "proc_major_faults": "Faults",
    "proc_invol_ctx": "Ctx Sw",
}


def _compute_light_tally(repos: list[dict[str, Any]], view: str) -> dict[str, dict[str, int]]:
    stoplight_key = f"stoplight_{view}"
    has_key = "has_data" if view == "system" else f"has_{view}"
    tally: dict[str, dict[str, int]] = {}
    for repo in repos:
        if not repo.get(has_key):
            continue
        for light_key, light in repo.get(stoplight_key, {}).get("lights", {}).items():
            bucket = tally.setdefault(light_key, {"green": 0, "yellow": 0, "red": 0, "unknown": 0})
            bucket[light] = bucket.get(light, 0) + 1
    return tally


def _render_light_tally(tally: dict[str, dict[str, int]]) -> str:
    if not tally:
        return ""
    chips = []
    for light_key, counts in tally.items():
        label = _METRIC_LABELS.get(light_key, light_key)
        parts = []
        for color in ("red", "yellow", "green"):
            n = counts.get(color, 0)
            if n:
                c = _LIGHT_COLOR[color]
                parts.append(f'<strong style="color:{c}">{n}</strong>')
        if not parts:
            continue
        chips.append(
            f'<span style="padding:3px 8px;border:1px solid #e2e8f0;border-radius:999px;'
            f'font-size:0.72rem;color:#64748b;background:white;white-space:nowrap">'
            f"{label}&nbsp;{'&thinsp;'.join(parts)}</span>"
        )
    if not chips:
        return ""
    return (
        '<div style="display:flex;gap:5px;flex-wrap:wrap;margin:10px 0 6px;'
        "padding:8px 12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;"
        'align-items:center">'
        '<span style="font-size:0.7rem;color:#94a3b8;margin-right:4px;white-space:nowrap">'
        "Lights:</span>" + "".join(chips) + "</div>"
    )


_VIEW_DESCRIPTIONS = {
    "system": "System-wide snapshots — CPU/mem/GPU from /proc &amp; nvidia-smi, averaged across all samples",
    "delta": "Resource change during <code>run</code> subcommand — how much the system shifted pre→post",
    "process": "Per-process-tree consumption via psutil — actual CPU &amp; RSS used by the spawned command",
}


def _tab_bar(current_view: str, port: int) -> str:
    tabs = []
    for view, label in _VIEW_LABELS.items():
        active = view == current_view
        bg = "#1e293b" if active else "#e2e8f0"
        color = "#e2e8f0" if active else "#475569"
        tabs.append(
            f'<a href="/?view={view}" style="padding:6px 16px;border-radius:6px;'
            f"background:{bg};color:{color};text-decoration:none;font-size:0.8rem;"
            f'font-weight:{"700" if active else "500"}">{label}</a>'
        )
    return '<div style="display:flex;gap:6px;flex-wrap:wrap">' + "".join(tabs) + "</div>"


def _render_system_row(
    repo: dict[str, Any],
    max_repo_bytes: float,
    next_run_ts: float = 0.0,
) -> str:
    s = repo["summary"]
    st = repo["stoplight_system"]
    lights = st.get("lights", {})
    metrics = st.get("metrics", {})
    overall = st.get("overall_light", "unknown")

    cpu = metrics.get("cpu_percent")
    load_ratio = metrics.get("loadavg_ratio")
    mem_ratio = metrics.get("memory_utilization_ratio")
    swap_ratio = metrics.get("swap_utilization_ratio")
    gpu = metrics.get("gpu_util_percent")
    gpu_mem_ratio = metrics.get("gpu_mem_utilization_ratio")
    repo_size = metrics.get("repo_size_bytes")
    mem_pct = mem_ratio * 100 if mem_ratio is not None else None
    swap_pct = swap_ratio * 100 if swap_ratio is not None else None
    gpu_mem_pct = gpu_mem_ratio * 100 if gpu_mem_ratio is not None else None
    load_pct = load_ratio * 100 if load_ratio is not None else None

    non_ignored_size = s.get("latest_git_non_ignored_size_bytes")
    tracked_size = s.get("latest_git_tracked_size_bytes")
    files = s.get("latest_git_tracked_file_count")
    dirty = s.get("latest_git_dirty_file_count")
    commits = s.get("latest_git_commit_count")
    dep_count = s.get("latest_dep_count")
    artefact_size = s.get("latest_artefact_size_bytes")

    _cpu_t = DEFAULT_THRESHOLDS["cpu_percent"]
    _load_t = DEFAULT_THRESHOLDS["loadavg_ratio"]
    _mem_t = DEFAULT_THRESHOLDS["memory_utilization_ratio"]
    _swap_t = DEFAULT_THRESHOLDS["swap_utilization_ratio"]
    _gpu_t = DEFAULT_THRESHOLDS["gpu_util_percent"]
    _gpu_mem_t = DEFAULT_THRESHOLDS["gpu_mem_utilization_ratio"]
    _sz_t = DEFAULT_THRESHOLDS["repo_size_bytes"]
    _art_t = DEFAULT_THRESHOLDS["artefact_size_bytes"]

    def _size_row(label: str, val: float | None, light: str = "unknown") -> str:
        bar = _gauge(val, max_repo_bytes, light, label=_fmt_bytes(val), **_sz_t)
        return f'<div style="font-size:0.68rem;color:#94a3b8;margin-top:4px">{label}</div>{bar}'

    def _sub_gauge(label: str, pct: float | None, light: str, t: dict) -> str:
        green_max_pct = t["green_max"] * 100
        yellow_max_pct = t["yellow_max"] * 100
        g = _gauge(pct, 100, light, green_max=green_max_pct, yellow_max=yellow_max_pct)
        return f'<div style="font-size:0.65rem;color:#94a3b8;margin-top:5px">{label}</div>{g}'

    dirty_span = f', <span style="color:#ef4444">{int(dirty)}\u2717</span>' if dirty else ""
    commit_span = (
        f'<div style="font-size:0.65rem;color:#94a3b8">{int(commits):,} commits</div>'
        if commits
        else ""
    )
    dep_span = (
        f'<div style="font-size:0.65rem;color:#94a3b8">{int(dep_count)} deps</div>'
        if dep_count
        else ""
    )
    repo_cell = (
        f'<div style="font-size:0.7rem;color:#64748b">'
        f"{int(files) if files else '—'} tracked{dirty_span}</div>" + commit_span + dep_span
    )

    art_light = light_max(artefact_size, **_art_t) if artefact_size else "unknown"
    size_cell = (
        _size_row("total", repo_size, lights.get("repo_size", "unknown"))
        + _size_row("non-ignored", non_ignored_size, light_max(non_ignored_size, **_sz_t))
        + _size_row("tracked", tracked_size, light_max(tracked_size, **_sz_t))
        + (
            f'<div style="font-size:0.68rem;color:#94a3b8;margin-top:4px">artefacts</div>'
            f"{_gauge(artefact_size, 1e9, art_light, label=_fmt_bytes(artefact_size), **_art_t)}"
            if artefact_size
            else ""
        )
    )

    mem_green = _mem_t["green_max"] * 100
    mem_yellow = _mem_t["yellow_max"] * 100
    mem_cell = _gauge(
        mem_pct, 100, lights.get("memory", "unknown"), green_max=mem_green, yellow_max=mem_yellow
    ) + _sub_gauge("swap", swap_pct, lights.get("swap", "unknown"), _swap_t)

    cpu_cell = _gauge(cpu, 100, lights.get("cpu", "unknown"), **_cpu_t) + _sub_gauge(
        "load avg", load_pct, lights.get("loadavg", "unknown"), _load_t
    )

    gpu_cell = _gauge(gpu, 100, lights.get("gpu", "unknown"), **_gpu_t) + _sub_gauge(
        "VRAM", gpu_mem_pct, lights.get("gpu_mem", "unknown"), _gpu_mem_t
    )

    return (
        f"<tr>"
        f"<td>{_name_cell(repo)}</td>"
        f"<td>{_dot(overall)}{_badge(overall)}</td>"
        f"<td>{cpu_cell}</td>"
        f"<td>{mem_cell}</td>"
        f"<td>{gpu_cell}</td>"
        f"<td>{size_cell}</td>"
        f"<td>{repo_cell}</td>"
        f"<td>{_schedule_cell(repo, next_run_ts)}</td>"
        f"</tr>"
    )


def _name_cell(repo: dict[str, Any]) -> str:
    """Repo name with a red backlog badge when there are open items."""
    name = repo["name"]
    count = repo.get("backlog_open", 0)
    if count:
        badge = (
            f'<span title="{count} open backlog item(s)" '
            f'style="background:#ef4444;color:white;border-radius:10px;'
            f"padding:1px 6px;font-size:0.65rem;font-weight:700;"
            f'margin-left:6px;vertical-align:middle">{count}</span>'
        )
        return f"<strong>{name}</strong>{badge}"
    return f"<strong>{name}</strong>"


def _no_run_cell(repo: dict[str, Any], colspan: int) -> str:
    no_run_reason = repo.get("no_run_reason", "")
    run_command = repo.get("run_command", "")
    if no_run_reason:
        msg = f'<span style="color:#94a3b8">{no_run_reason}</span>'
    elif run_command:
        msg = f"Awaiting first run &mdash; <code>{run_command}</code>"
    else:
        msg = "No run command configured"
    return (
        f"<tr><td>{_name_cell(repo)}</td>"
        f'<td colspan="{colspan}" style="font-size:0.8rem">{msg}</td></tr>'
    )


def _schedule_cell(repo: dict[str, Any], next_run_ts: float) -> str:
    """Render last-run and next-run schedule info for a repo."""
    now = time.time()
    last_ts = repo.get("last_run_ts")

    if last_ts is not None:
        last_rel = _fmt_rel_time(last_ts - now)  # negative → "X ago"
        last_str = f'<div style="font-size:0.72rem;color:#64748b">\u25b6 {last_rel}</div>'
    else:
        last_str = '<div style="font-size:0.72rem;color:#94a3b8">no data yet</div>'

    next_rel = _fmt_rel_time(next_run_ts - now)  # positive → "in X"
    next_str = (
        f'<div style="font-size:0.72rem;color:#475569;margin-top:3px">\u23f0 {next_rel}</div>'
    )
    return last_str + next_str


def _fmt_signed_bytes(v: float | None) -> str | None:
    if v is None:
        return None
    sign = "+" if v >= 0 else ""
    return f"{sign}{_fmt_bytes(abs(v))}"


def _render_delta_row(repo: dict[str, Any], next_run_ts: float = 0.0) -> str:
    if not repo["has_delta"]:
        return _no_run_cell(repo, colspan=8)

    st = repo["stoplight_delta"]
    lights = st.get("lights", {})
    metrics = st.get("metrics", {})
    overall = st.get("overall_light", "unknown")
    pair_count = repo["delta_summary"].get("pair_count", 0)

    cpu = metrics.get("avg_delta_cpu_percent")
    mem = metrics.get("avg_delta_memory_used_bytes")
    gpu = metrics.get("avg_delta_gpu_util_percent")
    disk_read = metrics.get("avg_delta_disk_io_read_bytes")
    disk_write = metrics.get("avg_delta_disk_io_write_bytes")
    net_recv = metrics.get("avg_delta_net_recv_bytes")
    net_sent = metrics.get("avg_delta_net_sent_bytes")

    cpu_label = f"{'+' if (cpu or 0) >= 0 else ''}{cpu:.1f}%" if cpu is not None else None
    gpu_label = f"{'+' if (gpu or 0) >= 0 else ''}{gpu:.1f}%" if gpu is not None else None

    def _clamp(v: float | None) -> float | None:
        return max(0.0, v) if v is not None else None

    _dt = DELTA_THRESHOLDS

    def _delta_sub(
        label: str, v: float | None, max_v: float, light_key: str, thresh_key: str
    ) -> str:
        g = _gauge(
            _clamp(v),
            max_v,
            lights.get(light_key, "unknown"),
            label=_fmt_bytes(v),
            **_dt[thresh_key],
        )
        return f'<div style="font-size:0.65rem;color:#94a3b8;margin-top:5px">{label}</div>{g}'

    cpu_gauge = _gauge(
        _clamp(cpu),
        100,
        lights.get("delta_cpu", "unknown"),
        label=cpu_label,
        **_dt["delta_cpu_percent"],
    )
    mem_gauge = _gauge(
        _clamp(mem),
        2e9,
        lights.get("delta_memory", "unknown"),
        label=_fmt_signed_bytes(mem),
        **_dt["delta_memory_used_bytes"],
    )
    gpu_gauge = _gauge(
        _clamp(gpu),
        100,
        lights.get("delta_gpu", "unknown"),
        label=gpu_label,
        **_dt["delta_gpu_util_percent"],
    )

    disk_cell = _delta_sub(
        "↓ read", disk_read, 5e9, "delta_disk_read", "delta_disk_io_read_bytes"
    ) + _delta_sub("↑ write", disk_write, 2e9, "delta_disk_write", "delta_disk_io_write_bytes")
    net_cell = _delta_sub(
        "↓ recv", net_recv, 500e6, "delta_net_recv", "delta_net_recv_bytes"
    ) + _delta_sub("↑ sent", net_sent, 500e6, "delta_net_sent", "delta_net_sent_bytes")

    return (
        f"<tr>"
        f"<td>{_name_cell(repo)}</td>"
        f"<td>{_dot(overall)}{_badge(overall)}</td>"
        f"<td>{cpu_gauge}</td>"
        f"<td>{mem_gauge}</td>"
        f"<td>{gpu_gauge}</td>"
        f"<td>{disk_cell}</td>"
        f"<td>{net_cell}</td>"
        f'<td style="color:#64748b;font-size:0.75rem">{pair_count} pairs</td>'
        f"<td>{_schedule_cell(repo, next_run_ts)}</td>"
        f"</tr>"
    )


def _fmt_runtime(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.1f}s"
    m, rem = divmod(int(s), 60)
    return f"{m}m {rem}s"


def _render_process_row(repo: dict[str, Any], next_run_ts: float = 0.0) -> str:
    if not repo["has_process"]:
        return _no_run_cell(repo, colspan=9)

    st = repo["stoplight_process"]
    lights = st.get("lights", {})
    metrics = st.get("metrics", {})
    overall = st.get("overall_light", "unknown")
    rs = repo["run_summary"]
    qualifying = rs.get("qualifying_run_count", 0)

    avg_cpu = metrics.get("avg_proc_cpu_percent")
    peak_cpu = metrics.get("avg_proc_peak_cpu_percent")
    avg_rss = metrics.get("avg_proc_memory_rss_bytes")
    peak_rss = metrics.get("avg_proc_peak_memory_rss_bytes")
    avg_runtime = metrics.get("avg_runtime_seconds")
    peak_threads = metrics.get("avg_proc_peak_thread_count")
    major_faults = metrics.get("avg_proc_major_faults")
    invol_ctx = metrics.get("avg_proc_involuntary_ctx_switches")
    energy_j = rs.get("avg_proc_energy_joules")
    latest_rt = rs.get("latest_runtime_seconds")
    run_count = rs.get("run_count", 0)
    fail_count = rs.get("fail_count", 0)
    last_failed_at = rs.get("last_failed_at")
    last_failed_rc = rs.get("last_failed_returncode")
    last_run_at = rs.get("last_run_at")

    rt_sublabel = (
        f'<div style="font-size:0.65rem;color:#94a3b8">latest {_fmt_runtime(latest_rt)}</div>'
        if latest_rt is not None
        else ""
    )

    _pt = PROCESS_THRESHOLDS
    rt_gauge = (
        _gauge(
            avg_runtime,
            300,
            lights.get("proc_runtime", "unknown"),
            label=_fmt_runtime(avg_runtime),
            **_pt["proc_avg_runtime_seconds"],
        )
        + rt_sublabel
    )

    def _extras_row(label: str, val: float | None, light: str, t: dict, fmt_fn=None) -> str:
        display = (
            fmt_fn(val)
            if (fmt_fn and val is not None)
            else (f"{int(val):,}" if val is not None else "—")
        )
        c = _LIGHT_COLOR.get(light, "#94a3b8")
        return (
            f'<div style="font-size:0.65rem;color:#94a3b8;margin-top:4px">{label}</div>'
            f'<div style="font-size:0.78rem;color:{c}">{display}</div>'
        )

    def _fmt_energy(j: float) -> str:
        return f"{j:.1f} J" if j >= 1.0 else f"{j * 1000:.0f} mJ"

    extras_cell = (
        _extras_row(
            "threads peak",
            peak_threads,
            lights.get("proc_threads", "unknown"),
            _pt["proc_peak_thread_count"],
        )
        + _extras_row(
            "major faults",
            major_faults,
            lights.get("proc_major_faults", "unknown"),
            _pt["proc_major_faults"],
        )
        + _extras_row(
            "invol ctx sw",
            invol_ctx,
            lights.get("proc_invol_ctx", "unknown"),
            _pt["proc_involuntary_ctx_switches"],
        )
        + _extras_row("energy", energy_j, "unknown", {}, fmt_fn=_fmt_energy)
    )

    # Last-run / failure summary appended below the numeric extras.
    if last_failed_at is not None:
        fail_color = "#ef4444"
        fail_label = f"exit {last_failed_rc}" if last_failed_rc is not None else "failed"
        extras_cell += (
            f'<div style="font-size:0.65rem;color:#94a3b8;margin-top:6px">last fail</div>'
            f'<div style="font-size:0.78rem;color:{fail_color}">'
            f"{_fmt_age(last_failed_at)} ({fail_label})</div>"
        )
    success_count = run_count - fail_count
    if run_count:
        rate_pct = round(success_count / run_count * 100)
        rate_color = "#22c55e" if rate_pct == 100 else ("#eab308" if rate_pct >= 80 else "#ef4444")
        extras_cell += (
            f'<div style="font-size:0.65rem;color:#94a3b8;margin-top:4px">success rate</div>'
            f'<div style="font-size:0.78rem;color:{rate_color}">'
            f"{rate_pct}% ({success_count}/{run_count})</div>"
        )
    last_run_suffix = ""
    if last_run_at:
        last_run_suffix = f"<br><span style='color:#94a3b8'>{_fmt_age(last_run_at)}</span>"

    return (
        f"<tr>"
        f"<td>{_name_cell(repo)}</td>"
        f"<td>{_dot(overall)}{_badge(overall)}</td>"
        f"<td>{_gauge(avg_cpu, 100, lights.get('proc_avg_cpu', 'unknown'), **_pt['proc_avg_cpu_percent'])}</td>"
        f"<td>{_gauge(peak_cpu, 100, lights.get('proc_peak_cpu', 'unknown'), **_pt['proc_peak_cpu_percent'])}</td>"
        f"<td>{_gauge(avg_rss, 4e9, lights.get('proc_avg_rss', 'unknown'), label=_fmt_bytes(avg_rss), **_pt['proc_avg_memory_rss_bytes'])}</td>"
        f"<td>{_gauge(peak_rss, 4e9, lights.get('proc_peak_rss', 'unknown'), label=_fmt_bytes(peak_rss), **_pt['proc_peak_memory_rss_bytes'])}</td>"
        f"<td>{rt_gauge}</td>"
        f"<td>{extras_cell}</td>"
        f'<td style="color:#64748b;font-size:0.75rem">{qualifying} runs'
        f"{last_run_suffix}"
        f"</td>"
        f"<td>{_schedule_cell(repo, next_run_ts)}</td>"
        f"</tr>"
    )


def _tail_log(log_path: Path | None, lines: int = 1) -> str:
    """Return the last N non-empty lines from a log file, HTML-escaped."""
    if not log_path or not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
        return " &mdash; ".join(
            ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for ln in tail
        )
    except OSError:
        return ""


def _run_button(view: str = "system") -> str:
    """Render the 'Run All Snapshots' button with current run state."""
    running = _RUN_STATE["running"]
    last_exit = _RUN_STATE["last_exit"]
    last_finished = _RUN_STATE["last_finished"]
    started_at = _RUN_STATE["started_at"]
    log_path: Path | None = _RUN_STATE["log_path"]

    if running:
        elapsed = int(time.time() - (started_at or time.time()))
        label = f"Running\u2026 {elapsed}s"
        btn_style = "background:#94a3b8;color:white;cursor:not-allowed"
        disabled = "disabled"
        last_line = _tail_log(log_path)
        status = (
            f'<span style="color:#94a3b8;font-size:0.72rem;margin-left:10px">{last_line}</span>'
            if last_line
            else ""
        )
    else:
        label = "Run All Snapshots"
        btn_style = "background:#1e293b;color:#e2e8f0;cursor:pointer"
        disabled = ""
        status = ""
        if last_finished is not None:
            finished_str = time.strftime("%H:%M:%S", time.localtime(last_finished))
            if last_exit == 0:
                status = f'<span style="color:#22c55e;font-size:0.75rem;margin-left:10px">\u2713 OK at {finished_str}</span>'
            else:
                last_line = _tail_log(log_path)
                detail = f" &mdash; {last_line}" if last_line else ""
                status = (
                    f'<span style="color:#ef4444;font-size:0.75rem;margin-left:10px">'
                    f"\u2717 Failed (exit {last_exit}) at {finished_str}{detail}</span>"
                )

    return (
        f'<form method="POST" action="/api/run-all?view={view}" style="display:inline">'
        f'<button type="submit" {disabled} style="padding:6px 14px;border:none;border-radius:6px;'
        f'font-size:0.8rem;font-weight:600;{btn_style}">{label}</button>'
        f"</form>{status}"
    )


def _render_dashboard(
    repos: list[dict[str, Any]],
    host: dict[str, Any],
    agent_utilization: dict[str, Any],
    fedora_debug: dict[str, Any],
    port: int,
    view: str = "system",
    tachometer_root: Path = Path("."),
) -> str:
    view = view if view in _VIEW_LABELS else "system"

    # Portfolio light — use the active view's repo stoplights
    stoplight_key = f"stoplight_{view}"
    has_key = f"has_{view}" if view != "system" else "has_data"
    all_lights = {r[stoplight_key].get("overall_light", "unknown") for r in repos if r.get(has_key)}
    portfolio_light = worst_light({i: light for i, light in enumerate(all_lights)})  # type: ignore[arg-type]
    portfolio_color = _LIGHT_COLOR.get(portfolio_light, "#94a3b8")
    portfolio_label = {
        "green": "All Systems Normal",
        "yellow": "Watch — Pressure Detected",
        "red": "Throttle — Overloaded",
        "unknown": "Awaiting Data",
    }.get(portfolio_light, "Awaiting Data")
    host_banner = _render_host_banner(host)
    agent_utilization_banner = _render_agent_utilization_banner(agent_utilization)
    fedora_debug_banner = _render_fedora_debug_banner(fedora_debug)
    tally_html = _render_light_tally(_compute_light_tally(repos, view))

    # Dynamic gauge scale — largest total repo size = 100% bar width.
    max_repo_bytes = (
        max(
            (r["summary"].get("latest_repo_size_bytes") or 0 for r in repos if r["has_data"]),
            default=1,
        )
        or 1
    )

    # Schedule: compute once, share across all rows
    schedule_hours = _load_schedule_hours(tachometer_root)
    next_run_ts = _next_schedule_ts(schedule_hours)

    # Column headers per view
    if view == "system":
        headers = "<th>Repository</th><th>Status</th><th>CPU % (sys)</th><th>Memory % (sys)</th><th>GPU % (sys)</th><th>Repo Size</th><th>Repo</th><th>Schedule</th>"
    elif view == "delta":
        headers = "<th>Repository</th><th>Status</th><th>ΔCPU %</th><th>ΔMemory</th><th>ΔGPU %</th><th>ΔDisk I/O</th><th>ΔNetwork</th><th>Pairs</th><th>Schedule</th>"
    else:
        headers = "<th>Repository</th><th>Status</th><th>Avg CPU %</th><th>Peak CPU %</th><th>Avg RSS</th><th>Peak RSS</th><th>Avg Runtime</th><th>Extras</th><th>Runs</th><th>Schedule</th>"

    rows = []
    current_category = None
    col_count = headers.count("<th>")
    for repo in repos:
        cat = repo["category"]
        if cat != current_category:
            current_category = cat
            rows.append(
                f'<tr><td colspan="{col_count}" style="background:#f1f5f9;font-size:0.7rem;'
                f"font-weight:700;color:#64748b;text-transform:uppercase;"
                f'letter-spacing:.08em;padding:6px 14px">{cat}</td></tr>'
            )

        if view == "system":
            if not repo["has_data"]:
                rows.append(
                    f"<tr><td>{_name_cell(repo)}</td>"
                    f'<td colspan="{col_count - 1}" style="color:#94a3b8;font-size:0.8rem">No data — '
                    f"run ./scripts/run_tachometer_profile.sh snapshot</td></tr>"
                )
                continue
            rows.append(_render_system_row(repo, max_repo_bytes, next_run_ts=next_run_ts))
        elif view == "delta":
            rows.append(_render_delta_row(repo, next_run_ts=next_run_ts))
        else:
            rows.append(_render_process_row(repo, next_run_ts=next_run_ts))

    rows_html = "\n".join(rows)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    tab_bar = _tab_bar(view, port)
    view_desc = _VIEW_DESCRIPTIONS[view]
    run_btn = _run_button(view)
    refresh_interval = 3 if _RUN_STATE["running"] else 60

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh_interval}">
<title>Tachometer — Portfolio Dashboard</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        background:#f8fafc;color:#1e293b;padding:24px;font-size:14px}}
  h1{{font-size:1.4rem;font-weight:700}}
  .sub{{color:#64748b;font-size:0.8rem;margin-top:2px;margin-bottom:12px}}
  .desc{{color:#64748b;font-size:0.78rem;margin-bottom:14px;font-style:italic}}
  .banner{{display:flex;align-items:center;gap:12px;padding:12px 18px;
           border-radius:10px;background:white;margin-bottom:22px;
           border:2px solid {portfolio_color};box-shadow:0 1px 3px rgba(0,0,0,.07)}}
  .bdot{{width:18px;height:18px;border-radius:50%;background:{portfolio_color};flex-shrink:0}}
  .blabel{{font-size:1rem;font-weight:600;color:{portfolio_color}}}
  table{{width:100%;border-collapse:collapse;background:white;border-radius:10px;
         box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}}
  th{{background:#1e293b;color:#e2e8f0;padding:9px 14px;text-align:left;
      font-size:0.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em}}
  td{{padding:9px 14px;border-bottom:1px solid #f1f5f9;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#f8fafc}}
  .foot{{color:#94a3b8;font-size:0.7rem;text-align:right;margin-top:10px}}
  a{{color:#3b82f6}}
  code{{background:#f1f5f9;padding:1px 4px;border-radius:3px;font-size:0.75rem}}
</style>
</head>
<body>
<h1>&#127950; Tachometer</h1>
<div class="sub">
  Portfolio resource monitor &middot; auto-refreshes every 60 s &middot;
  <a href="/api/status">JSON API</a>
</div>
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
  {tab_bar}
  {run_btn}
</div>
<div class="desc">{view_desc}</div>
<div class="banner">
  <div style="display:flex;align-items:center;gap:12px;justify-content:space-between;flex-wrap:wrap;width:100%">
    <div style="display:flex;align-items:center;gap:12px">
      <div class="bdot"></div>
      <div>
        <div class="blabel">{portfolio_label}</div>
        <div style="font-size:0.78rem;color:#64748b">Portfolio {view} aggregate</div>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
      {host_banner}
      {agent_utilization_banner}
      {fedora_debug_banner}
    </div>
  </div>
</div>
{tally_html}
<table>
<thead>
<tr>
  {headers}
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
<div class="foot">Updated {now} &middot; :{port}</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


def _build_api_payload(
    repos: list[dict[str, Any]],
    host: dict[str, Any],
    agent_utilization: dict[str, Any],
    fedora_debug: dict[str, Any],
) -> dict[str, Any]:
    all_lights = {
        r["stoplight_system"].get("overall_light", "unknown") for r in repos if r["has_data"]
    }
    portfolio_light = worst_light({i: light for i, light in enumerate(all_lights)})  # type: ignore[arg-type]
    return {
        "timestamp": time.time(),
        "system_light": portfolio_light,
        "system_backoff_action": backoff_action(portfolio_light),
        "portfolio_light": portfolio_light,
        "portfolio_backoff_action": backoff_action(portfolio_light),
        "host_light": host["stoplight_host"].get("overall_light", "unknown"),
        "host": {
            "has_data": host["has_data"],
            "summary": host["summary"],
            "stoplight": host["stoplight_host"],
        },
        "agent_utilization_light": agent_utilization.get("overall_light", "unknown"),
        "agent_utilization": {
            "has_data": agent_utilization["has_data"],
            "snapshot": agent_utilization["snapshot"],
            "overall_light": agent_utilization.get("overall_light", "unknown"),
        },
        "fedora_debug_light": fedora_debug.get("overall_light", "unknown"),
        "fedora_debug": {
            "has_data": fedora_debug["has_data"],
            "signals": fedora_debug["signals"],
            "overall_light": fedora_debug.get("overall_light", "unknown"),
        },
        "repos": [
            {
                "name": r["name"],
                "category": r["category"],
                "has_data": r["has_data"],
                "has_delta": r["has_delta"],
                "has_process": r["has_process"],
                "stoplight_system": r["stoplight_system"],
                "stoplight_delta": r["stoplight_delta"],
                "stoplight_process": r["stoplight_process"],
                "backlog_open": r["backlog_open"],
                "backlog": r["backlog"],
            }
            for r in repos
        ],
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    tachometer_root: Path = Path(".")
    host_summary_path: Path = Path(".tachometer/host-summary.json")
    port: int = 5100

    def _send(
        self, body: str, content_type: str = "text/html; charset=utf-8", status: int = 200
    ) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        view = qs.get("view", ["system"])[0]

        repos = gather_repo_data(self.__class__.tachometer_root)
        host = gather_host_data(self.__class__.host_summary_path)
        agent_utilization = gather_agent_utilization_data(
            _agent_utilization_sidecar_path(self.__class__.tachometer_root)
        )
        fedora_debug = gather_fedora_debug_data(
            _fedora_debug_sidecar_path(self.__class__.tachometer_root)
        )
        if parsed.path == "/api/status":
            self._send(
                json.dumps(
                    _build_api_payload(repos, host, agent_utilization, fedora_debug),
                    indent=2,
                ),
                "application/json",
            )
        elif parsed.path in ("/", "/index.html"):
            self._send(
                _render_dashboard(
                    repos,
                    host,
                    agent_utilization,
                    fedora_debug,
                    self.__class__.port,
                    view=view,
                    tachometer_root=self.__class__.tachometer_root,
                )
            )
        else:
            self._send("Not Found", "text/plain", 404)

    def do_POST(self) -> None:
        if not _same_origin_request(self.headers):
            self._send("Forbidden", "text/plain", 403)
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        view = qs.get("view", ["system"])[0]
        if parsed.path == "/api/run-all":
            _start_snapshot_run(self.__class__.tachometer_root)
            # Redirect back to dashboard (303 See Other so browser does a GET)
            self.send_response(303)
            self.send_header("Location", f"/?view={view}")
            self.end_headers()
        else:
            self._send("Not Found", "text/plain", 404)

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
        pass


def serve(
    tachometer_root: Path,
    *,
    host: str = "127.0.0.1",
    host_summary_path: Path | None = None,
    port: int = 5100,
    allow_remote: bool = False,
) -> None:
    """Start the dashboard HTTP server (blocking)."""
    _validate_bind_host(host, allow_remote=allow_remote)
    handler = type(
        "Handler",
        (_Handler,),
        {
            "tachometer_root": tachometer_root,
            "host_summary_path": host_summary_path
            or (tachometer_root / ".tachometer" / "host-summary.json"),
            "port": port,
        },
    )
    httpd = HTTPServer((host, port), handler)
    print(f"Tachometer dashboard : http://{host}:{port}/")
    print(f"JSON status API      : http://{host}:{port}/api/status")
    print("Press Ctrl-C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()
