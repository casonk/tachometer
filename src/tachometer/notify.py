"""Shock-relay notification hook for tachometer red-light events.

When a new backlog entry is opened (metric first hits red), this module
dispatches a message via the shock-relay service scripts.  All failures are
suppressed so a misconfigured or absent shock-relay installation never breaks
a snapshot or run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_SERVICE_SCRIPTS: dict[str, str] = {
    "telegram": "services/telegram/send_message.py",
    "signal": "services/signal-cli/send_message.py",
    "twilio": "services/twilio/send_sms.py",
    "whatsapp": "services/whatsapp/send_message.py",
}


def _format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def format_alert(
    entry: dict[str, Any],
    repo_name: str | None = None,
) -> str:
    """Return a concise alert message for a newly-opened red-light entry."""
    repo_part = f"{repo_name}: " if repo_name else ""
    light_id = entry.get("id", f"{entry.get('view', '?')}.{entry.get('light_key', '?')}")
    value_str = _format_value(entry.get("value"))
    suggestions = entry.get("suggestions", [])
    suggestion = (
        suggestions[0] if suggestions else "Investigate and reduce this resource's utilisation."
    )
    return f"[tachometer] RED — {repo_part}{light_id}\nValue: {value_str}\n{suggestion}"


def send_red_light_alert(
    entry: dict[str, Any],
    *,
    shock_relay_root: str | Path,
    service: str,
    target: str,
    config_path: str | None = None,
    repo_name: str | None = None,
) -> bool:
    """Dispatch a red-light alert via shock-relay. Returns True on success."""
    script_rel = _SERVICE_SCRIPTS.get(service)
    if not script_rel:
        return False
    script = Path(shock_relay_root) / script_rel
    if not script.exists():
        return False

    message = format_alert(entry, repo_name=repo_name)
    cmd = [sys.executable, str(script), target, message]
    if config_path:
        cmd += ["--config", config_path]

    # Inject the script's directory into PYTHONPATH so its sibling common.py resolves.
    env = {**os.environ, "PYTHONPATH": str(script.parent)}
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, env=env)
        return result.returncode == 0
    except Exception:
        return False


def notify_new_red_lights(
    newly_opened: list[dict[str, Any]],
    *,
    shock_relay_root: str | Path,
    service: str,
    target: str,
    config_path: str | None = None,
    repo_name: str | None = None,
) -> None:
    """Send an alert for each entry in newly_opened. Failures are silenced."""
    for entry in newly_opened:
        send_red_light_alert(
            entry,
            shock_relay_root=shock_relay_root,
            service=service,
            target=target,
            config_path=config_path,
            repo_name=repo_name,
        )
