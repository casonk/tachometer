"""Tachometer admin settings — retention caps and other tunables.

Settings live in ``config/tachometer/settings.toml`` next to the manifest.
All values have safe defaults so missing keys never break a run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

_DEFAULTS: dict[str, Any] = {
    "retention": {
        "sample_days": 365,
        "run_days": 365,
        "backlog_resolved_days": 365,
    }
}

_SETTINGS_FILENAME = "settings.toml"


def settings_path(manifest_path: str | Path) -> Path:
    """Return the settings.toml path sibling to a manifest file."""
    return Path(manifest_path).parent / _SETTINGS_FILENAME


def load_settings(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Merge defaults for any missing keys.
    merged: dict[str, Any] = {}
    for section, defaults in _DEFAULTS.items():
        merged[section] = {**defaults, **data.get(section, {})}
    return merged


def save_settings(path: str | Path, settings: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for section, values in settings.items():
        lines.append(f"[{section}]")
        for key, val in values.items():
            lines.append(f"{key} = {val}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def retention(settings: dict[str, Any]) -> dict[str, int]:
    r = settings.get("retention", _DEFAULTS["retention"])
    return {
        "sample_days": int(r.get("sample_days", 365)),
        "run_days": int(r.get("run_days", 365)),
        "backlog_resolved_days": int(r.get("backlog_resolved_days", 365)),
    }
