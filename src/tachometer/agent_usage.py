from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _compact_number(value: int | float | None) -> str:
    if value is None:
        return "—"
    absolute = abs(float(value))
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _run_command(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    return proc.returncode, "\n".join(part for part in (stdout, stderr) if part)


def _parse_json_output(command: tuple[str, ...]) -> dict[str, Any]:
    rc, output = _run_command(*command)
    if rc != 0 or not output:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(output)
    return {}


def _limit_light(*percentages: float | int | None) -> str:
    numeric = [float(value) for value in percentages if isinstance(value, int | float)]
    if not numeric:
        return "unknown"
    max_used = max(numeric)
    if max_used >= 90:
        return "red"
    if max_used >= 70:
        return "yellow"
    return "green"


def _latest_by_date(entries: list[dict[str, Any]]) -> dict[str, Any]:
    dated = [entry for entry in entries if isinstance(entry.get("date"), str)]
    if not dated:
        return {}
    return max(dated, key=lambda entry: str(entry["date"]))


def _latest_file(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def _collect_claude(home: Path) -> dict[str, Any]:
    installed = shutil.which("claude") is not None
    stats_path = home / ".claude" / "stats-cache.json"
    stats = _load_json(stats_path)
    auth = _parse_json_output(("claude", "auth", "status", "--json")) if installed else {}

    latest_daily = _latest_by_date(list(stats.get("dailyModelTokens", [])))
    latest_day = latest_daily.get("date")
    tokens_by_model = latest_daily.get("tokensByModel", {}) if isinstance(latest_daily, dict) else {}
    latest_total_tokens = sum(
        int(value) for value in tokens_by_model.values() if isinstance(value, int | float)
    )
    top_model = None
    top_model_tokens = None
    token_items = [
        (str(model), int(tokens))
        for model, tokens in tokens_by_model.items()
        if isinstance(tokens, int | float)
    ]
    if token_items:
        top_model, top_model_tokens = max(
            token_items,
            key=lambda item: item[1],
        )

    status = "not_installed"
    summary = "CLI not installed"
    if installed and stats:
        status = "usage_available"
        summary = (
            f"{_compact_number(latest_total_tokens)} on {latest_day}"
            if latest_total_tokens and latest_day
            else "usage cache present"
        )
    elif installed and bool(auth.get("loggedIn")):
        status = "auth_only"
        summary = "authenticated, awaiting usage cache"

    return {
        "provider": "claude",
        "display_name": "Claude",
        "installed": installed,
        "has_data": bool(stats),
        "logged_in": bool(auth.get("loggedIn")) if auth else None,
        "status": status,
        "light": "unknown",
        "summary": summary,
        "notes": [
            "Local auth status does not currently expose quota percentages.",
        ],
        "details": {
            "subscription_type": auth.get("subscriptionType"),
            "api_provider": auth.get("apiProvider"),
            "org_name": auth.get("orgName"),
            "last_computed_date": stats.get("lastComputedDate"),
            "latest_day": latest_day,
            "latest_daily_tokens_total": latest_total_tokens or None,
            "latest_daily_tokens_by_model": tokens_by_model if isinstance(tokens_by_model, dict) else {},
            "top_model": top_model,
            "top_model_tokens": top_model_tokens,
            "total_sessions": stats.get("totalSessions"),
            "total_messages": stats.get("totalMessages"),
            "model_usage": stats.get("modelUsage", {}),
        },
    }


def _collect_codex(home: Path) -> dict[str, Any]:
    installed = shutil.which("codex") is not None
    session_candidates = list((home / ".codex" / "sessions").rglob("*.jsonl"))
    session_candidates.extend((home / ".codex" / "archived_sessions").glob("*.jsonl"))
    session_path = _latest_file(session_candidates)

    rc, login_output = _run_command("codex", "login", "status") if installed else (1, "")
    logged_in = installed and rc == 0 and "Logged in" in login_output

    last_turn_context: dict[str, Any] | None = None
    last_token_count: dict[str, Any] | None = None
    if session_path is not None:
        with session_path.open(encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                with contextlib.suppress(json.JSONDecodeError):
                    record = json.loads(raw_line)
                    record_type = record.get("type")
                    if record_type == "turn_context":
                        payload = record.get("payload")
                        if isinstance(payload, dict):
                            last_turn_context = payload
                    elif record_type == "event_msg":
                        payload = record.get("payload")
                        if isinstance(payload, dict) and payload.get("type") == "token_count":
                            last_token_count = {
                                "timestamp": record.get("timestamp"),
                                "info": payload.get("info"),
                                "rate_limits": payload.get("rate_limits"),
                            }

    model = None
    if isinstance(last_turn_context, dict):
        model = last_turn_context.get("model")
    info = last_token_count.get("info") if isinstance(last_token_count, dict) else {}
    if not isinstance(info, dict):
        info = {}
    rate_limits = last_token_count.get("rate_limits") if isinstance(last_token_count, dict) else {}
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    primary = rate_limits.get("primary", {}) if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = (
        rate_limits.get("secondary", {}) if isinstance(rate_limits.get("secondary"), dict) else {}
    )
    total_usage = (
        info.get("total_token_usage", {}) if isinstance(info.get("total_token_usage"), dict) else {}
    )
    last_usage = (
        info.get("last_token_usage", {}) if isinstance(info.get("last_token_usage"), dict) else {}
    )
    primary_used = primary.get("used_percent")
    secondary_used = secondary.get("used_percent")
    light = _limit_light(primary_used, secondary_used)

    status = "not_installed"
    summary = "CLI not installed"
    if installed and last_token_count:
        status = "usage_available"
        percent_bits = []
        if isinstance(primary_used, int | float):
            percent_bits.append(f"P{primary_used:.0f}%")
        if isinstance(secondary_used, int | float):
            percent_bits.append(f"S{secondary_used:.0f}%")
        if isinstance(total_usage.get("total_tokens"), int | float):
            percent_bits.append(f"{_compact_number(total_usage['total_tokens'])} session toks")
        summary = " / ".join(percent_bits) if percent_bits else "rate-limit data available"
    elif installed and logged_in:
        status = "auth_only"
        summary = "authenticated, awaiting session"

    return {
        "provider": "codex",
        "display_name": "Codex",
        "installed": installed,
        "has_data": bool(last_token_count),
        "logged_in": logged_in,
        "status": status,
        "light": light,
        "summary": summary,
        "notes": [],
        "details": {
            "model": model,
            "plan_type": rate_limits.get("plan_type"),
            "event_timestamp": last_token_count.get("timestamp")
            if isinstance(last_token_count, dict)
            else None,
            "total_token_usage": total_usage,
            "last_token_usage": last_usage,
            "rate_limits": rate_limits,
        },
    }


def _copilot_session_tokens(model_metrics: dict[str, Any]) -> int | None:
    total = 0
    found = False
    for metrics in model_metrics.values():
        if not isinstance(metrics, dict):
            continue
        usage = metrics.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
            value = usage.get(key)
            if isinstance(value, int | float):
                total += int(value)
                found = True
    return total if found else None


def _collect_copilot(home: Path) -> dict[str, Any]:
    installed = shutil.which("copilot") is not None
    event_files = sorted(
        (home / ".copilot" / "session-state").glob("*/events.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    latest_shutdown: dict[str, Any] | None = None
    latest_shutdown_ts = ""
    latest_file_pending = False
    for index, path in enumerate(event_files):
        last_event_type = None
        last_shutdown = None
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                with contextlib.suppress(json.JSONDecodeError):
                    record = json.loads(raw_line)
                    last_event_type = record.get("type")
                    if record.get("type") == "session.shutdown":
                        last_shutdown = record
        if index == 0 and last_event_type != "session.shutdown":
            latest_file_pending = True
        if last_shutdown is None:
            continue
        timestamp = str(last_shutdown.get("timestamp", ""))
        if not latest_shutdown or timestamp > latest_shutdown_ts:
            latest_shutdown = last_shutdown
            latest_shutdown_ts = timestamp

    shutdown_data = latest_shutdown.get("data", {}) if isinstance(latest_shutdown, dict) else {}
    model_metrics = (
        shutdown_data.get("modelMetrics", {})
        if isinstance(shutdown_data.get("modelMetrics"), dict)
        else {}
    )
    session_tokens = _copilot_session_tokens(model_metrics)
    premium_requests = shutdown_data.get("totalPremiumRequests")

    status = "not_installed"
    summary = "CLI not installed"
    notes: list[str] = []
    if installed and latest_shutdown:
        status = "usage_available"
        summary_bits = []
        if isinstance(premium_requests, int | float):
            summary_bits.append(f"{int(premium_requests)} premium")
        if session_tokens is not None:
            summary_bits.append(f"{_compact_number(session_tokens)} session toks")
        if isinstance(shutdown_data.get("currentModel"), str):
            summary_bits.append(str(shutdown_data["currentModel"]))
        summary = " / ".join(summary_bits) if summary_bits else "session usage available"
        if latest_file_pending:
            notes.append("A newer open session exists; usage refreshes after session shutdown.")
    elif installed and event_files:
        status = "awaiting_session_shutdown"
        summary = "session open; usage lands on shutdown"
        notes.append("Open a Copilot session and let it shut down to capture usage totals.")
    elif installed:
        status = "awaiting_session"
        summary = "open a session to populate usage"
        notes.append("Copilot utilization is only available after a session has started.")

    return {
        "provider": "copilot",
        "display_name": "Copilot",
        "installed": installed,
        "has_data": bool(latest_shutdown),
        "logged_in": None,
        "status": status,
        "light": "unknown",
        "summary": summary,
        "notes": notes,
        "details": {
            "event_timestamp": latest_shutdown.get("timestamp")
            if isinstance(latest_shutdown, dict)
            else None,
            "current_model": shutdown_data.get("currentModel"),
            "total_premium_requests": premium_requests,
            "total_api_duration_ms": shutdown_data.get("totalApiDurationMs"),
            "session_tokens": session_tokens,
            "current_tokens": shutdown_data.get("currentTokens"),
            "system_tokens": shutdown_data.get("systemTokens"),
            "conversation_tokens": shutdown_data.get("conversationTokens"),
            "tool_definitions_tokens": shutdown_data.get("toolDefinitionsTokens"),
            "model_metrics": model_metrics,
        },
    }


def collect_agent_utilization(*, home: Path | None = None) -> dict[str, Any]:
    home_dir = home or Path.home()
    providers = {
        "claude": _collect_claude(home_dir),
        "codex": _collect_codex(home_dir),
        "copilot": _collect_copilot(home_dir),
    }
    codex_light = providers["codex"].get("light", "unknown")
    if codex_light in {"green", "yellow", "red"}:
        overall_light = codex_light
    elif any(bool(provider.get("has_data")) for provider in providers.values()):
        overall_light = "green"
    else:
        overall_light = "unknown"
    return {
        "captured_at": time.time(),
        "overall_light": overall_light,
        "providers": providers,
    }
