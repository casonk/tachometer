"""The dashboard's Service latency page: latest reply times, trend, alert state, startup timings.

Reads the files ``tachometer service-latency`` writes; the buttons start the same probe and
startup measurement in the background (the dashboard serves one request at a time, and a probe
can take a minute when a service hangs).
"""

from __future__ import annotations

import contextlib
import html
import json
import threading
import time
from pathlib import Path
from typing import Any

from . import service_latency as sl

TREND_POINTS = 48
CONFIG_RELATIVE = Path("config") / "tachometer" / sl.CONFIG_NAME

_STATE: dict[str, Any] = {"running": "", "started_at": 0.0, "message": ""}
_LOCK = threading.Lock()


def build_payload(root: Path) -> dict[str, Any]:
    history = sl.load_history(sl.history_path(root))
    names: list[str] = []
    for record in history:
        if record.get("name") and record["name"] not in names:
            names.append(record["name"])
    alerts: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        alerts = json.loads(sl.state_path(root).read_text(encoding="utf-8"))
    config = sl.load_config(root / CONFIG_RELATIVE) if (root / CONFIG_RELATIVE).exists() else None
    runs = config["settings"]["baseline_runs"] if config else sl.DEFAULTS["baseline_runs"]
    if config:  # a service dropped from the config keeps its old samples but leaves the page
        names = [name for name in names if name in {s.name for s in config["services"]}]
    services = []
    for name in names:
        records = [r for r in history if r.get("name") == name]
        latest = records[-1]
        services.append(
            {
                "name": name,
                "latest": latest,
                "baseline_ms": sl.baseline_ms(history, name, runs),
                "trend": [r.get("reply_p50_ms") for r in records[-TREND_POINTS:]],
                "bad": bool(alerts.get(name, {}).get("bad")),
                "reason": alerts.get(name, {}).get("reason", ""),
            }
        )
    services.sort(key=lambda s: -(s["latest"].get("reply_p50_ms") or 0))
    return {
        "generated_at": time.time(),
        "services": services,
        "startups": list(reversed(sl.load_history(sl.startup_path(root))))[:10],
        "configured": [s.name for s in config["services"]] if config else [],
        "running": dict(_STATE),
    }


def _ms(value: Any) -> str:
    return "–" if value is None else f"{value:.0f} ms" if value >= 10 else f"{value:.1f} ms"


def _spark(values: list[Any], bad: bool) -> str:
    points = [v for v in values if v is not None]
    if len(points) < 2:
        return ""
    low, high = min(points), max(points)
    span = (high - low) or 1.0
    width, height = 120, 22
    coords = " ".join(
        f"{i * width / (len(points) - 1):.1f},{height - 2 - (v - low) / span * (height - 4):.1f}"
        for i, v in enumerate(points)
    )
    colour = "var(--red)" if bad else "var(--accent)"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'aria-label="reply time trend"><polyline fill="none" stroke="{colour}" stroke-width="1.5" '
        f'points="{coords}"/></svg>'
    )


def render_page(root: Path, *, message: str | None = None, message_ok: bool = True) -> str:
    data = build_payload(root)
    rows = []
    for service in data["services"]:
        latest = service["latest"]
        failing = not latest.get("ok", True)
        light = "red" if service["bad"] or failing else "green"
        detail = html.escape(service["reason"] or latest.get("error") or "")
        age = latest.get("unit_age_s")
        rows.append(
            f'<tr><td><span class="dot {light}"></span> {html.escape(service["name"])}'
            f"{f'<div class=sub>{detail}</div>' if detail else ''}</td>"
            f"<td>{_ms(latest.get('reply_p50_ms'))}</td><td>{_ms(latest.get('reply_p95_ms'))}</td>"
            f"<td>{_ms(latest.get('connect_p50_ms'))}</td><td>{_ms(service['baseline_ms'])}</td>"
            f"<td>{_spark(service['trend'], light == 'red')}</td>"
            f"<td>{'–' if age is None else f'{age / 3600:.1f} h'}</td>"
            f'<td><form method="post" action="/latency/startup"><input type="hidden" name="service" '
            f'value="{html.escape(service["name"], quote=True)}"><button type="submit" title="Wait for this '
            f'service to listen and reply (run it right after a restart)">Time startup</button></form></td></tr>'
        )
    startups = "".join(
        f"<tr><td>{html.escape(time.strftime('%Y-%m-%d %H:%M', time.localtime(s['ts'])))}</td>"
        f"<td>{html.escape(str(s.get('name', '')))}</td>"
        f"<td>{'already up (≤ ' + str(s.get('ready_within_s')) + ' s)' if s.get('already_ready') else _secs(s.get('time_to_listening_s'))}</td>"
        f"<td>{'' if s.get('already_ready') else _secs(s.get('time_to_ready_s'))}</td>"
        f"<td>{html.escape(str(s.get('since', '')))}</td></tr>"
        for s in data["startups"]
    )
    running = data["running"]["running"]
    banner = ""
    if message:
        banner = f'<div class="msg {"ok" if message_ok else "bad"}">{html.escape(message)}</div>'
    elif running:
        banner = f'<div class="msg">{html.escape(running)} is running; reload in a moment.</div>'
    body = (
        "".join(rows)
        or '<tr><td colspan="8" class="empty">No samples yet. Run <code>tachometer service-latency probe</code> or press Probe now.</td></tr>'
    )
    return (
        _PAGE.replace("__BANNER__", banner)
        .replace("__ROWS__", body)
        .replace(
            "__STARTUPS__",
            startups or '<tr><td colspan="5" class="empty">No startup timings yet.</td></tr>',
        )
        .replace("__COUNT__", str(len(data["services"])))
        .replace("__BAD__", str(sum(1 for s in data["services"] if s["bad"])))
    )


def _secs(value: Any) -> str:
    return "–" if value is None else f"{value:.1f} s"


# --------------------------------------------------------------------------- actions


def _start(label: str, work) -> tuple[bool, str]:
    with _LOCK:
        if _STATE["running"]:
            return False, f"{_STATE['running']} is already running"
        _STATE.update(running=label, started_at=time.time(), message="")

    def run() -> None:
        try:
            work()
        finally:
            with _LOCK:
                _STATE["running"] = ""

    threading.Thread(target=run, name="latency-action", daemon=True).start()
    return True, f"{label} started; reload in a moment"


def start_probe(root: Path) -> tuple[bool, str]:
    """Probe every configured service in the background without touching the alert state."""
    config_path = root / CONFIG_RELATIVE
    if not config_path.exists():
        return False, f"no {sl.CONFIG_NAME}; copy service-latency.toml.example first"
    config = sl.load_config(config_path)
    if not config["services"]:
        return False, "no services configured"
    return _start("Probe", lambda: sl.run_probe(root, config, alerts=False))


def start_startup(root: Path, name: str, *, timeout: float = 120.0) -> tuple[bool, str]:
    """Time one service's start-up in the background; the result lands in the startup table."""
    config_path = root / CONFIG_RELATIVE
    config = sl.load_config(config_path) if config_path.exists() else {"services": []}
    service = next((s for s in config["services"] if s.name == name), None)
    if service is None:
        return False, f"{name!r} is not a configured service"

    def work() -> None:
        sl.record_startup(root, sl.measure_startup(service, timeout=timeout), now=time.time())

    return _start(f"Startup timing for {name}", work)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tachometer · Service latency</title>
<link rel="stylesheet" href="/_portfolio/theme.css">
<script src="/_portfolio/theme.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 40px;background:var(--bg);color:var(--text);
 font-family:var(--font,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif);font-size:var(--font-size,14px)}
main{max-width:1000px;margin:0 auto}
h1{font-size:1.25rem;margin:0 0 2px}h2{font-size:.95rem;margin:18px 0 6px}
.sub{color:var(--muted);font-size:.78rem}
.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.8rem;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.green{background:var(--green)}.red{background:var(--red)}
button{font:inherit;padding:4px 12px;border:1px solid var(--text);border-radius:6px;background:var(--text);
 color:var(--surface);cursor:pointer}
form{margin:0;display:inline}
.msg{padding:8px 12px;border-radius:8px;margin-bottom:12px;font-size:.85rem;border:1px solid var(--border)}
.msg.bad{border-color:var(--red)}
.empty{color:var(--muted);text-align:center;padding:24px 0}
a{color:inherit}code{background:var(--bg);padding:1px 4px;border-radius:3px}
</style>
<script src="/_portfolio/chrome.js" defer></script>
</head><body><main>
<div class="top"><div><h1>Service latency</h1>
<div class="sub">__COUNT__ services, __BAD__ alerting &middot; reply time is the median of a few loopback requests
 with no credentials &middot; <a href="/">Dashboard</a> &middot; <a href="/api/latency">JSON</a></div></div>
<form method="post" action="/latency/probe"><button type="submit">Probe now</button></form></div>
__BANNER__
<div class="card"><table><thead><tr><th>Service</th><th>Reply p50</th><th>p95</th><th>Connect</th><th>Baseline</th>
<th>Trend</th><th>Up for</th><th></th></tr></thead><tbody>__ROWS__</tbody></table></div>
<h2>Recent startup timings</h2>
<div class="card"><table><thead><tr><th>When</th><th>Service</th><th>Listening</th><th>Ready</th><th>Counted from</th></tr></thead>
<tbody>__STARTUPS__</tbody></table></div>
<p class="sub">Press <b>Time startup</b> right after restarting a service: it waits for the service to listen and reply and
 counts from the unit's own start. Press it on a service that is already up and it reports only an upper bound.
 A manual probe records samples but leaves alerting to the scheduled run.</p>
</main></body></html>
"""
