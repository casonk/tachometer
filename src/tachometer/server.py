"""Portfolio-wide stoplight dashboard server.

Serves a human-readable gauge page at / and a machine-readable JSON status
feed at /api/status.  Red-light repos are flagged for throttle / hard-backoff
so that consuming services can self-regulate.

Usage (via CLI):
    tachometer serve --manifest config/tachometer/profile.toml --port 5100
"""
from __future__ import annotations

import contextlib
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from .stoplight import backoff_action, worst_light
from .stoplight import evaluate as stoplight_evaluate

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
        summary = _load_json(repo_dir / ".tachometer" / "summary.json")
        has_data = bool(summary.get("sample_count", 0))
        stoplight = stoplight_evaluate(summary) if has_data else {}
        results.append({
            "name": repo["name"],
            "category": _category_from_path(path_str),
            "has_data": has_data,
            "summary": summary,
            "stoplight": stoplight,
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


def _gauge(value: float | None, max_val: float, light: str) -> str:
    if value is None:
        return '<span style="color:#94a3b8">—</span>'
    pct = min(100.0, value / max_val * 100.0)
    c = _LIGHT_COLOR.get(light, "#94a3b8")
    return (
        f'<div style="font-size:0.78rem">{value:.1f}</div>'
        f'<div style="width:80px;height:6px;background:#e2e8f0;border-radius:3px;margin-top:2px">'
        f'<div style="width:{pct:.1f}%;height:6px;background:{c};border-radius:3px"></div></div>'
    )


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

def _render_dashboard(repos: list[dict[str, Any]], port: int) -> str:
    all_lights = {r["stoplight"].get("overall_light", "unknown") for r in repos if r["has_data"]}
    system_light = worst_light({i: light for i, light in enumerate(all_lights)})  # type: ignore[arg-type]
    system_color = _LIGHT_COLOR.get(system_light, "#94a3b8")
    system_label = {
        "green": "All Systems Normal",
        "yellow": "Watch — Pressure Detected",
        "red": "Throttle — Overloaded",
        "unknown": "Awaiting Data",
    }.get(system_light, "Awaiting Data")

    rows = []
    current_category = None
    for repo in repos:
        cat = repo["category"]
        if cat != current_category:
            current_category = cat
            rows.append(
                f'<tr><td colspan="7" style="background:#f1f5f9;font-size:0.7rem;'
                f'font-weight:700;color:#64748b;text-transform:uppercase;'
                f'letter-spacing:.08em;padding:6px 14px">{cat}</td></tr>'
            )

        if not repo["has_data"]:
            rows.append(
                f'<tr><td><strong>{repo["name"]}</strong></td>'
                f'<td colspan="6" style="color:#94a3b8;font-size:0.8rem">No data — '
                f'run ./scripts/run_tachometer_profile.sh snapshot</td></tr>'
            )
            continue

        s = repo["summary"]
        st = repo["stoplight"]
        lights = st.get("lights", {})
        metrics = st.get("metrics", {})
        overall = st.get("overall_light", "unknown")

        cpu = metrics.get("cpu_percent")
        mem_ratio = metrics.get("memory_utilization_ratio")
        disk_ratio = metrics.get("disk_utilization_ratio")
        gpu = metrics.get("gpu_util_percent")

        mem_pct = mem_ratio * 100 if mem_ratio is not None else None
        disk_pct = disk_ratio * 100 if disk_ratio is not None else None

        files = s.get("latest_git_tracked_file_count")
        dirty = s.get("latest_git_dirty_file_count")
        size = _fmt_bytes(s.get("latest_repo_size_bytes"))
        dirty_span = (
            f', <span style="color:#ef4444">{int(dirty)}✗</span>' if dirty else ""
        )
        repo_cell = (
            f'<div style="font-size:0.78rem">{size}</div>'
            f'<div style="font-size:0.7rem;color:#64748b">'
            f'{int(files) if files else "—"} tracked{dirty_span}</div>'
        )

        rows.append(
            f'<tr>'
            f'<td><strong>{repo["name"]}</strong></td>'
            f'<td>{_dot(overall)}{_badge(overall)}</td>'
            f'<td>{_gauge(cpu, 100, lights.get("cpu","unknown"))}</td>'
            f'<td>{_gauge(mem_pct, 100, lights.get("memory","unknown"))}</td>'
            f'<td>{_gauge(disk_pct, 100, lights.get("disk","unknown"))}</td>'
            f'<td>{_gauge(gpu, 100, lights.get("gpu","unknown"))}</td>'
            f'<td>{repo_cell}</td>'
            f'</tr>'
        )

    rows_html = "\n".join(rows)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

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
  .sub{{color:#64748b;font-size:0.8rem;margin-top:2px;margin-bottom:20px}}
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
</style>
</head>
<body>
<h1>🏎 Tachometer</h1>
<div class="sub">
  Portfolio resource monitor &middot; auto-refreshes every 60 s &middot;
  <a href="/api/status">JSON API</a>
</div>
<div class="banner">
  <div class="bdot"></div>
  <div class="blabel">{system_label}</div>
</div>
<table>
<thead>
<tr>
  <th>Repository</th><th>Status</th>
  <th>CPU %</th><th>Memory %</th><th>Disk % (sys)</th><th>GPU %</th>
  <th>Repo</th>
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
    all_lights = {r["stoplight"].get("overall_light", "unknown") for r in repos if r["has_data"]}
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
                "stoplight": r["stoplight"],
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
        repos = gather_repo_data(self.__class__.tachometer_root)
        if self.path == "/api/status":
            self._send(json.dumps(_build_api_payload(repos), indent=2), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(_render_dashboard(repos, self.__class__.port))
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
