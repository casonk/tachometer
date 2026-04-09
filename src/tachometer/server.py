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
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from .profile import summarize_delta_pairs, summarize_run_records
from .stoplight import (
    DEFAULT_THRESHOLDS,
    backoff_action,
    evaluate_delta,
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
}


def _start_snapshot_run(tachometer_root: Path) -> bool:
    """Launch run_all_tachometer_snapshots.sh in the background.

    Returns True if the run was started, False if one is already in progress.
    """
    with _RUN_LOCK:
        if _RUN_STATE["running"]:
            return False
        _RUN_STATE["running"] = True
        _RUN_STATE["started_at"] = time.time()
        _RUN_STATE["last_exit"] = None

    script = tachometer_root / "scripts" / "run_all_tachometer_snapshots.sh"

    def _run() -> None:
        proc = subprocess.Popen(
            ["bash", str(script)],
            cwd=str(tachometer_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
    repos.append({
        "name": "tachometer",
        "path": "./util-repos/tachometer",
        "reason": "Self-profile.",
    })

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

        results.append({
            "name": repo["name"],
            "category": _category_from_path(path_str),
            "has_data": has_data,
            "has_delta": has_delta,
            "has_process": has_process,
            "summary": summary,
            "delta_summary": delta_summary,
            "run_summary": run_summary,
            "stoplight_system": stoplight_system,
            "stoplight_delta": stoplight_delta,
            "stoplight_process": stoplight_process,
        })

    return sorted(results, key=lambda r: (r["category"], r["name"]))


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


def _gauge(value: float | None, max_val: float, light: str, label: str | None = None) -> str:
    if value is None:
        return '<span style="color:#94a3b8">—</span>'
    pct = min(100.0, value / max_val * 100.0)
    c = _LIGHT_COLOR.get(light, "#94a3b8")
    display = label if label is not None else f"{value:.1f}"
    return (
        f'<div style="font-size:0.78rem">{display}</div>'
        f'<div style="width:80px;height:6px;background:#e2e8f0;border-radius:3px;margin-top:2px">'
        f'<div style="width:{pct:.1f}%;height:6px;background:{c};border-radius:3px"></div></div>'
    )


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_VIEW_LABELS = {
    "system": "System (sys)",
    "delta": "Delta (pre→post)",
    "process": "Process (psutil)",
}

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
            f'background:{bg};color:{color};text-decoration:none;font-size:0.8rem;'
            f'font-weight:{"700" if active else "500"}">{label}</a>'
        )
    return (
        '<div style="display:flex;gap:6px;flex-wrap:wrap">'
        + "".join(tabs)
        + "</div>"
    )


def _render_system_row(
    repo: dict[str, Any],
    max_repo_bytes: float,
) -> str:
    s = repo["summary"]
    st = repo["stoplight_system"]
    lights = st.get("lights", {})
    metrics = st.get("metrics", {})
    overall = st.get("overall_light", "unknown")

    cpu = metrics.get("cpu_percent")
    mem_ratio = metrics.get("memory_utilization_ratio")
    gpu = metrics.get("gpu_util_percent")
    repo_size = metrics.get("repo_size_bytes")
    mem_pct = mem_ratio * 100 if mem_ratio is not None else None

    non_ignored_size = s.get("latest_git_non_ignored_size_bytes")
    tracked_size = s.get("latest_git_tracked_size_bytes")
    files = s.get("latest_git_tracked_file_count")
    dirty = s.get("latest_git_dirty_file_count")

    def _size_row(label: str, val: float | None, light: str = "unknown") -> str:
        bar = _gauge(val, max_repo_bytes, light, label=_fmt_bytes(val))
        return (
            f'<div style="font-size:0.68rem;color:#94a3b8;margin-top:4px">{label}</div>'
            f"{bar}"
        )

    dirty_span = (
        f', <span style="color:#ef4444">{int(dirty)}\u2717</span>' if dirty else ""
    )
    repo_cell = (
        f'<div style="font-size:0.7rem;color:#64748b">'
        f'{int(files) if files else "—"} tracked{dirty_span}</div>'
    )

    _sz_t = DEFAULT_THRESHOLDS["repo_size_bytes"]
    size_cell = (
        _size_row("total", repo_size, lights.get("repo_size", "unknown"))
        + _size_row("non-ignored", non_ignored_size, light_max(non_ignored_size, **_sz_t))
        + _size_row("tracked", tracked_size, light_max(tracked_size, **_sz_t))
    )

    return (
        f"<tr>"
        f"<td><strong>{repo['name']}</strong></td>"
        f"<td>{_dot(overall)}{_badge(overall)}</td>"
        f"<td>{_gauge(cpu, 100, lights.get('cpu', 'unknown'))}</td>"
        f"<td>{_gauge(mem_pct, 100, lights.get('memory', 'unknown'))}</td>"
        f"<td>{_gauge(gpu, 100, lights.get('gpu', 'unknown'))}</td>"
        f"<td>{size_cell}</td>"
        f"<td>{repo_cell}</td>"
        f"</tr>"
    )


def _render_delta_row(repo: dict[str, Any]) -> str:
    if not repo["has_delta"]:
        return (
            f"<tr><td><strong>{repo['name']}</strong></td>"
            f'<td colspan="4" style="color:#94a3b8;font-size:0.8rem">No pre/post pairs — '
            f"run <code>tachometer run ...</code></td></tr>"
        )

    st = repo["stoplight_delta"]
    lights = st.get("lights", {})
    metrics = st.get("metrics", {})
    overall = st.get("overall_light", "unknown")
    pair_count = repo["delta_summary"].get("pair_count", 0)

    cpu = metrics.get("avg_delta_cpu_percent")
    mem = metrics.get("avg_delta_memory_used_bytes")
    gpu = metrics.get("avg_delta_gpu_util_percent")

    cpu_label = f"{'+' if (cpu or 0) >= 0 else ''}{cpu:.1f}%" if cpu is not None else None
    mem_label = f"{'+' if (mem or 0) >= 0 else ''}{_fmt_bytes(abs(mem) if mem else None)}" if mem is not None else None
    gpu_label = f"{'+' if (gpu or 0) >= 0 else ''}{gpu:.1f}%" if gpu is not None else None

    cpu_gauge = _gauge(max(0.0, cpu) if cpu is not None else None, 100, lights.get("delta_cpu", "unknown"), label=cpu_label)
    mem_gauge = _gauge(max(0.0, mem) if mem is not None else None, 2e9, lights.get("delta_memory", "unknown"), label=mem_label)
    gpu_gauge = _gauge(max(0.0, gpu) if gpu is not None else None, 100, lights.get("delta_gpu", "unknown"), label=gpu_label)

    return (
        f"<tr>"
        f"<td><strong>{repo['name']}</strong></td>"
        f"<td>{_dot(overall)}{_badge(overall)}</td>"
        f"<td>{cpu_gauge}</td>"
        f"<td>{mem_gauge}</td>"
        f"<td>{gpu_gauge}</td>"
        f'<td style="color:#64748b;font-size:0.75rem">{pair_count} pairs</td>'
        f"</tr>"
    )


def _render_process_row(repo: dict[str, Any]) -> str:
    if not repo["has_process"]:
        return (
            f"<tr><td><strong>{repo['name']}</strong></td>"
            f'<td colspan="5" style="color:#94a3b8;font-size:0.8rem">No psutil data — '
            f"install psutil and run <code>tachometer run ...</code></td></tr>"
        )

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

    return (
        f"<tr>"
        f"<td><strong>{repo['name']}</strong></td>"
        f"<td>{_dot(overall)}{_badge(overall)}</td>"
        f"<td>{_gauge(avg_cpu, 100, lights.get('proc_avg_cpu', 'unknown'))}</td>"
        f"<td>{_gauge(peak_cpu, 100, lights.get('proc_peak_cpu', 'unknown'))}</td>"
        f"<td>{_gauge(avg_rss, 4e9, lights.get('proc_avg_rss', 'unknown'), label=_fmt_bytes(avg_rss))}</td>"
        f"<td>{_gauge(peak_rss, 4e9, lights.get('proc_peak_rss', 'unknown'), label=_fmt_bytes(peak_rss))}</td>"
        f'<td style="color:#64748b;font-size:0.75rem">{qualifying} runs</td>'
        f"</tr>"
    )


def _run_button(view: str = "system") -> str:
    """Render the 'Run All Snapshots' button with current run state."""
    running = _RUN_STATE["running"]
    last_exit = _RUN_STATE["last_exit"]
    last_finished = _RUN_STATE["last_finished"]
    started_at = _RUN_STATE["started_at"]

    if running:
        elapsed = int(time.time() - (started_at or time.time()))
        label = f"Running\u2026 {elapsed}s"
        btn_style = "background:#94a3b8;color:white;cursor:not-allowed"
        disabled = "disabled"
    else:
        label = "Run All Snapshots"
        btn_style = "background:#1e293b;color:#e2e8f0;cursor:pointer"
        disabled = ""

    status = ""
    if not running and last_finished is not None:
        finished_str = time.strftime("%H:%M:%S", time.localtime(last_finished))
        if last_exit == 0:
            status = f'<span style="color:#22c55e;font-size:0.75rem;margin-left:10px">\u2713 OK at {finished_str}</span>'
        else:
            status = f'<span style="color:#ef4444;font-size:0.75rem;margin-left:10px">\u2717 Failed (exit {last_exit}) at {finished_str}</span>'

    return (
        f'<form method="POST" action="/api/run-all?view={view}" style="display:inline">'
        f'<button type="submit" {disabled} style="padding:6px 14px;border:none;border-radius:6px;'
        f'font-size:0.8rem;font-weight:600;{btn_style}">{label}</button>'
        f"</form>{status}"
    )


def _render_dashboard(repos: list[dict[str, Any]], port: int, view: str = "system") -> str:
    view = view if view in _VIEW_LABELS else "system"

    # Overall system light — use the active view's stoplights
    stoplight_key = f"stoplight_{view}"
    has_key = f"has_{view}" if view != "system" else "has_data"
    all_lights = {
        r[stoplight_key].get("overall_light", "unknown")
        for r in repos
        if r.get(has_key)
    }
    system_light = worst_light({i: light for i, light in enumerate(all_lights)})  # type: ignore[arg-type]
    system_color = _LIGHT_COLOR.get(system_light, "#94a3b8")
    system_label = {
        "green": "All Systems Normal",
        "yellow": "Watch — Pressure Detected",
        "red": "Throttle — Overloaded",
        "unknown": "Awaiting Data",
    }.get(system_light, "Awaiting Data")

    # Disk is a system-level metric — extract from first repo with data for banner.
    system_disk_pct: float | None = None
    system_disk_light = "unknown"
    for r in repos:
        if r["has_data"]:
            dr = r["stoplight_system"].get("metrics", {}).get("disk_utilization_ratio")
            if dr is not None:
                system_disk_pct = dr * 100
                system_disk_light = r["stoplight_system"].get("lights", {}).get("disk", "unknown")
            break
    disk_banner = ""
    if system_disk_pct is not None:
        dc = _LIGHT_COLOR.get(system_disk_light, "#94a3b8")
        disk_banner = (
            f'<span style="margin-left:18px;padding-left:18px;border-left:1px solid #e2e8f0;'
            f'font-size:0.85rem;color:#475569">Disk&nbsp;'
            f'<strong style="color:{dc}">{system_disk_pct:.1f}%</strong></span>'
        )

    # Dynamic gauge scale — largest total repo size = 100% bar width.
    max_repo_bytes = max(
        (r["summary"].get("latest_repo_size_bytes") or 0 for r in repos if r["has_data"]),
        default=1,
    ) or 1

    # Column headers per view
    if view == "system":
        headers = "<th>Repository</th><th>Status</th><th>CPU % (sys)</th><th>Memory % (sys)</th><th>GPU % (sys)</th><th>Repo Size</th><th>Repo</th>"
    elif view == "delta":
        headers = "<th>Repository</th><th>Status</th><th>ΔCPU %</th><th>ΔMemory</th><th>ΔGPU %</th><th>Pairs</th>"
    else:
        headers = "<th>Repository</th><th>Status</th><th>Avg CPU %</th><th>Peak CPU %</th><th>Avg RSS</th><th>Peak RSS</th><th>Runs</th>"

    rows = []
    current_category = None
    col_count = headers.count("<th>")
    for repo in repos:
        cat = repo["category"]
        if cat != current_category:
            current_category = cat
            rows.append(
                f'<tr><td colspan="{col_count}" style="background:#f1f5f9;font-size:0.7rem;'
                f'font-weight:700;color:#64748b;text-transform:uppercase;'
                f'letter-spacing:.08em;padding:6px 14px">{cat}</td></tr>'
            )

        if view == "system":
            if not repo["has_data"]:
                rows.append(
                    f"<tr><td><strong>{repo['name']}</strong></td>"
                    f'<td colspan="{col_count - 1}" style="color:#94a3b8;font-size:0.8rem">No data — '
                    f"run ./scripts/run_tachometer_profile.sh snapshot</td></tr>"
                )
                continue
            rows.append(_render_system_row(repo, max_repo_bytes))
        elif view == "delta":
            rows.append(_render_delta_row(repo))
        else:
            rows.append(_render_process_row(repo))

    rows_html = "\n".join(rows)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    tab_bar = _tab_bar(view, port)
    view_desc = _VIEW_DESCRIPTIONS[view]
    run_btn = _run_button(view)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
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
           border:2px solid {system_color};box-shadow:0 1px 3px rgba(0,0,0,.07)}}
  .bdot{{width:18px;height:18px;border-radius:50%;background:{system_color};flex-shrink:0}}
  .blabel{{font-size:1rem;font-weight:600;color:{system_color}}}
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
  <div class="bdot"></div>
  <div class="blabel">{system_label}</div>
  {disk_banner}
</div>
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

def _build_api_payload(repos: list[dict[str, Any]]) -> dict[str, Any]:
    all_lights = {
        r["stoplight_system"].get("overall_light", "unknown") for r in repos if r["has_data"]
    }
    system_light = worst_light({i: light for i, light in enumerate(all_lights)})  # type: ignore[arg-type]
    return {
        "timestamp": time.time(),
        "system_light": system_light,
        "system_backoff_action": backoff_action(system_light),
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
            }
            for r in repos
        ],
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    tachometer_root: Path = Path(".")
    port: int = 5100

    def _send(self, body: str, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
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
        if parsed.path == "/api/status":
            self._send(json.dumps(_build_api_payload(repos), indent=2), "application/json")
        elif parsed.path in ("/", "/index.html"):
            self._send(_render_dashboard(repos, self.__class__.port, view=view))
        else:
            self._send("Not Found", "text/plain", 404)

    def do_POST(self) -> None:
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


def serve(tachometer_root: Path, port: int = 5100) -> None:
    """Start the dashboard HTTP server (blocking)."""
    handler = type(
        "Handler",
        (_Handler,),
        {"tachometer_root": tachometer_root, "port": port},
    )
    httpd = HTTPServer(("0.0.0.0", port), handler)
    print(f"Tachometer dashboard : http://localhost:{port}/")
    print(f"JSON status API      : http://localhost:{port}/api/status")
    print("Press Ctrl-C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()
