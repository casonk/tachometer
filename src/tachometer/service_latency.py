"""Per-service latency: how fast each local service listens, becomes ready and replies.

The numbers exist to catch the failure that a green health check hides: a service that is
"active" but slow, such as a gate that runs sixteen sequential lookups before it opens its
port. Two measurements, both standard library and portable (Linux, macOS, Windows):

``probe``
    Per service, ``samples`` TCP connects and HTTP GETs against loopback with no credentials.
    A gated site answers with its login redirect, which is the reply time that matters; any
    status below 500 counts as replying, a 5xx or a refused connection counts as failing.
    One record per service is appended to ``.tachometer/service-latency.jsonl``.

``startup``
    Polls until the service listens and replies, and reports time to listening and time to
    ready measured from the unit's own start time when the platform exposes it (``systemctl
    show`` on Linux), else from the moment the command began. Run it right after a restart.

Alerts compare a probe with the median of the service's previous records: slower than
``factor`` times that baseline (and by at least ``min_delta_ms``), or failing for
``consecutive`` runs in a row. An alert fires when a service turns bad and once more when
it recovers, never on every run while it stays bad.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

HISTORY_NAME = "service-latency.jsonl"
STATE_NAME = "service-latency-state.json"
CONFIG_NAME = "service-latency.local.toml"
DEFAULTS: dict[str, Any] = {
    "samples": 5,
    "timeout_seconds": 5.0,
    "baseline_runs": 20,
    "factor": 2.0,
    "min_delta_ms": 200.0,
    "consecutive": 2,
    "keep_days": 90,
}


@dataclass(frozen=True)
class Service:
    name: str
    host: str = "127.0.0.1"
    port: int = 0
    path: str = "/"
    unit: str = ""  # systemd unit used for its start time, optional
    headers: dict[str, str] = field(default_factory=dict)  # e.g. a Host header for a virtual host


# --------------------------------------------------------------------------- config


def history_path(root: Path) -> Path:
    return root / ".tachometer" / HISTORY_NAME


def state_path(root: Path) -> Path:
    return root / ".tachometer" / STATE_NAME


def _number(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number > minimum else default


def load_config(path: Path) -> dict[str, Any]:
    """Settings and services from the TOML file; unusable values fall back to defaults."""
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    settings = {
        "samples": int(_number(data.get("samples"), DEFAULTS["samples"], minimum=0.5)),
        "timeout_seconds": _number(data.get("timeout_seconds"), DEFAULTS["timeout_seconds"]),
        "baseline_runs": int(
            _number(data.get("baseline_runs"), DEFAULTS["baseline_runs"], minimum=0.5)
        ),
        "factor": _number(data.get("factor"), DEFAULTS["factor"], minimum=1.0),
        "min_delta_ms": _number(data.get("min_delta_ms"), DEFAULTS["min_delta_ms"]),
        "consecutive": int(_number(data.get("consecutive"), DEFAULTS["consecutive"], minimum=0.5)),
        "keep_days": int(_number(data.get("keep_days"), DEFAULTS["keep_days"], minimum=0.5)),
    }
    services: dict[str, Service] = {}
    registry = data.get("registry")
    if registry:
        for service in services_from_registry(Path(str(registry)).expanduser()):
            services[service.name] = service
    for raw in data.get("services", []):
        name = str(raw.get("name", "")).strip()
        port = int(raw.get("port", 0) or 0)
        if not name or not 0 < port < 65536:
            continue
        services[name] = Service(
            name=name,
            host=str(raw.get("host", "127.0.0.1")),
            port=port,
            path=str(raw.get("path", "/")) or "/",
            unit=str(raw.get("unit", "")),
            headers={str(k): str(v) for k, v in dict(raw.get("headers", {})).items()},
        )
    notify = data.get("notify") if isinstance(data.get("notify"), dict) else {}
    return {"settings": settings, "services": list(services.values()), "notify": notify}


def services_from_registry(services_toml: Path) -> list[Service]:
    """Loopback services from a wiring-harness ``services.toml`` merged with its ``.local`` overlay."""
    if not services_toml.exists():
        return []
    data = tomllib.loads(services_toml.read_text(encoding="utf-8"))
    overlay = services_toml.with_name(services_toml.stem + ".local.toml")
    by_name = {s["name"]: dict(s) for s in data.get("services", []) if "name" in s}
    if overlay.exists():
        for raw in tomllib.loads(overlay.read_text(encoding="utf-8")).get("services", []):
            if "name" in raw:
                by_name.setdefault(raw["name"], {}).update(raw)
    return [
        Service(name=name, port=int(site["port"]))
        for name, site in by_name.items()
        if isinstance(site.get("port"), int) and 0 < site["port"] < 65536
    ]


# --------------------------------------------------------------------------- probing


@dataclass
class Sample:
    connect_ms: float | None
    reply_ms: float | None
    status: int | None
    error: str = ""


def probe_once(service: Service, timeout: float, clock=time.perf_counter) -> Sample:
    """One TCP connect and one GET; never raises."""
    start = clock()
    try:
        with socket.create_connection((service.host, service.port), timeout=timeout):
            pass
    except OSError as exc:
        return Sample(None, None, None, f"connect: {exc.__class__.__name__}: {exc}"[:200])
    connect_ms = (clock() - start) * 1000
    conn = http.client.HTTPConnection(service.host, service.port, timeout=timeout)
    begin = clock()
    try:
        conn.request(
            "GET", service.path, headers={"User-Agent": "tachometer-latency", **service.headers}
        )
        response = conn.getresponse()
        response.read(1024)
        return Sample(connect_ms, (clock() - begin) * 1000, response.status)
    except (OSError, http.client.HTTPException) as exc:
        return Sample(connect_ms, None, None, f"reply: {exc.__class__.__name__}: {exc}"[:200])
    finally:
        conn.close()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def summarize(
    service: Service, samples: list[Sample], *, now: float, unit_age_s: float | None
) -> dict[str, Any]:
    replies = [s.reply_ms for s in samples if s.reply_ms is not None]
    connects = [s.connect_ms for s in samples if s.connect_ms is not None]
    statuses = [s.status for s in samples if s.status is not None]
    failing = [s for s in samples if s.status is None or s.status >= 500]
    errors = sorted({s.error for s in samples if s.error})
    return {
        "ts": now,
        "name": service.name,
        "port": service.port,
        "samples": len(samples),
        "failed": len(failing),
        "ok": not failing,
        "status": max(set(statuses), key=statuses.count) if statuses else None,
        "connect_p50_ms": round(statistics.median(connects), 2) if connects else None,
        "reply_p50_ms": round(statistics.median(replies), 2) if replies else None,
        "reply_p95_ms": round(_percentile(replies, 0.95), 2) if replies else None,
        "reply_max_ms": round(max(replies), 2) if replies else None,
        "unit_age_s": unit_age_s,
        "error": errors[0] if errors else "",
    }


# --------------------------------------------------------------------------- unit start time


def unit_age_seconds(unit: str, *, run=subprocess.run, now: float | None = None) -> float | None:
    """Seconds since the unit entered the active state, where the platform can say (systemd)."""
    if not unit or not sys.platform.startswith("linux"):
        return None
    for scope in ([], ["--user"]):
        try:
            result = run(
                [
                    "systemctl",
                    *scope,
                    "show",
                    unit,
                    "-p",
                    "ActiveState",
                    "-p",
                    "ActiveEnterTimestampMonotonic",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if props.get("ActiveState") == "active":
            with contextlib.suppress(ValueError):
                entered = int(props.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
                if entered > 0:
                    return round(time.monotonic() - entered, 1)
    return None


# --------------------------------------------------------------------------- history and alerts


def load_history(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
    return records


def append_history(
    path: Path, records: list[dict[str, Any]], *, keep_days: int, now: float
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = now - keep_days * 86400
    kept = [r for r in load_history(path) if float(r.get("ts", 0)) >= cutoff] + records
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept), encoding="utf-8"
    )
    temporary.replace(path)


def baseline_ms(history: list[dict[str, Any]], name: str, runs: int) -> float | None:
    """Median reply time over the service's last healthy ``runs`` records."""
    values = [
        float(r["reply_p50_ms"])
        for r in history
        if r.get("name") == name and r.get("ok") and r.get("reply_p50_ms") is not None
    ][-runs:]
    return statistics.median(values) if len(values) >= 3 else None


def evaluate(
    record: dict[str, Any], history: list[dict[str, Any]], settings: dict[str, Any]
) -> tuple[str, str]:
    """``("bad"|"pending"|"ok", reason)``. A failure is only ``bad`` once it has repeated ``consecutive`` runs."""
    name = record["name"]
    if not record["ok"]:
        needed = settings["consecutive"] - 1
        earlier = [r for r in history if r.get("name") == name][-needed:] if needed else []
        what = record["error"] or f"HTTP {record['status']}"
        if len(earlier) == needed and all(not r.get("ok") for r in earlier):
            return "bad", f"failing {settings['consecutive']} runs in a row: {what}"
        return "pending", what
    base = baseline_ms(history, name, settings["baseline_runs"])
    reply = record.get("reply_p50_ms")
    if (
        base is not None
        and reply is not None
        and reply > base * settings["factor"]
        and reply - base >= settings["min_delta_ms"]
    ):
        return "bad", f"reply {reply:.0f} ms vs {base:.0f} ms baseline"
    return "ok", ""


def update_alert_state(
    path: Path, verdicts: dict[str, tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """Transitions since the last run as ``(name, "open"|"recovered", reason)``; steady states stay quiet."""
    state: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        state = loaded if isinstance(loaded, dict) else {}
    events: list[tuple[str, str, str]] = []
    for name, (status, reason) in verdicts.items():
        was_bad = bool(state.get(name, {}).get("bad"))
        if status == "pending":
            continue  # undecided: keep whatever was alerted before
        if status == "bad" and not was_bad:
            events.append((name, "open", reason))
        elif status == "ok" and was_bad:
            events.append((name, "recovered", ""))
        state[name] = {"bad": status == "bad", "reason": reason}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return events


# --------------------------------------------------------------------------- commands


def run_probe(
    root: Path, config: dict[str, Any], *, now: float | None = None, probe=probe_once
) -> dict[str, Any]:
    settings = config["settings"]
    now = time.time() if now is None else now
    history = load_history(history_path(root))
    records, verdicts = [], {}
    for service in config["services"]:
        samples = [probe(service, settings["timeout_seconds"]) for _ in range(settings["samples"])]
        record = summarize(service, samples, now=now, unit_age_s=unit_age_seconds(service.unit))
        verdicts[service.name] = evaluate(record, history, settings)
        status, reason = verdicts[service.name]
        record["alert"] = reason if status == "bad" else ""
        records.append(record)
    append_history(history_path(root), records, keep_days=settings["keep_days"], now=now)
    events = update_alert_state(state_path(root), verdicts)
    return {"records": records, "events": events}


def measure_startup(
    service: Service,
    *,
    timeout: float = 120.0,
    interval: float = 0.1,
    clock=time.monotonic,
    sleep=time.sleep,
    probe=probe_once,
    age=unit_age_seconds,
) -> dict[str, Any]:
    """Time until the service listens and replies, counted from the unit's start when known."""
    started = clock()
    already_s = age(service.unit) or 0.0
    listening: float | None = None
    ready: float | None = None
    first = True
    ready_at_first_probe = False
    while clock() - started < timeout:
        sample = probe(service, min(2.0, timeout))
        elapsed = clock() - started
        if sample.connect_ms is not None and listening is None:
            listening = elapsed
        if sample.status is not None and sample.status < 500:
            ready = elapsed
            ready_at_first_probe = first
            break
        first = False
        sleep(interval)
    base = {
        "name": service.name,
        "unit_age_at_start_s": round(already_s, 2) if already_s else None,
        "ready": ready is not None,
    }
    if ready_at_first_probe:
        # It was up before we looked, so its start-up time is unknowable; the unit age is only an upper bound.
        return {
            **base,
            "already_ready": True,
            "ready_within_s": round(already_s, 2) if already_s else None,
            "time_to_listening_s": None,
            "time_to_ready_s": None,
            "since": "unit start" if already_s else "command start",
        }
    return {
        **base,
        "already_ready": False,
        "time_to_listening_s": round(listening + already_s, 2) if listening is not None else None,
        "time_to_ready_s": round(ready + already_s, 2) if ready is not None else None,
        "since": "unit start" if already_s else "command start",
    }


def alert_messages(
    events: list[tuple[str, str, str]], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Entries shaped for ``notify.send_red_light_alert``."""
    by_name = {r["name"]: r for r in records}
    entries = []
    for name, kind, reason in events:
        record = by_name.get(name, {})
        entries.append(
            {
                "id": f"service-latency.{name}",
                "value": record.get("reply_p50_ms"),
                "suggestions": [f"{name}: {'recovered' if kind == 'recovered' else reason}"],
            }
        )
    return entries
