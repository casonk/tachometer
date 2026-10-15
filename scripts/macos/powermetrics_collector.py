#!/usr/bin/python3
"""Root-run macOS collector: one `powermetrics` reading, appended as JSON.

The host sampler runs as the user and cannot read CPU temperature, fan speed
or thermal pressure on macOS: `powermetrics` needs root. This script is the
root half, the same split SMART uses on Linux. A LaunchDaemon runs it every
five minutes; the sampler reads the newest line of its output, pointed at by
TACHOMETER_MACOS_POWERMETRICS.

It is installed as a root-owned copy (see install_powermetrics_collector.sh),
never run from the user-writable checkout. It uses only the standard library
and stays compatible with the Python 3.9 that ships with macOS.

What `powermetrics` reports depends on the Mac. Intel Macs have the `smc`
sampler (CPU and GPU die temperature, fan speed). Apple Silicon has no
temperature sampler; it reports thermal pressure and CPU/GPU power. Samplers
the machine rejects are dropped and the reading is retried with the rest.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

POWERMETRICS = "/usr/bin/powermetrics"
DEFAULT_OUTPUT = "/var/db/tachometer/powermetrics.jsonl"
# One line every five minutes: a week of history is plenty for a collector
# whose reader only ever wants the newest line.
KEEP_LINES = 2016
SAMPLER_SETS = (
    ("smc", "thermal", "cpu_power", "gpu_power"),
    ("thermal", "cpu_power", "gpu_power"),
    ("thermal",),
)


def run_powermetrics(samplers, runner=subprocess.run) -> dict[str, Any] | None:
    """One plist sample from powermetrics, or None if it refuses the samplers."""
    try:
        out = runner(
            [
                POWERMETRICS,
                "-n",
                "1",
                "-i",
                "1000",
                "-f",
                "plist",
                "--samplers",
                ",".join(samplers),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    # Samples are NUL-separated plist documents; -n 1 gives exactly one.
    first = out.stdout.split(b"\0", 1)[0].strip()
    try:
        data = plistlib.loads(first)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _number(value: Any) -> float | None:
    # A tuple, not int | float: this runs under the Python 3.9 macOS ships.
    if isinstance(value, bool) or not isinstance(value, (int, float)):  # noqa: UP038
        return None
    return float(value)


def _watts(milliwatts: Any) -> float | None:
    value = _number(milliwatts)
    return round(value / 1000.0, 2) if value is not None else None


def reduce_sample(data: dict[str, Any], samplers) -> dict[str, Any]:
    """The few numbers the thermal page uses, from one powermetrics plist."""
    smc = data.get("smc") if isinstance(data.get("smc"), dict) else {}
    processor = data.get("processor") if isinstance(data.get("processor"), dict) else {}
    gpu = data.get("gpu") if isinstance(data.get("gpu"), dict) else {}
    fan = _number(smc.get("fan"))
    pressure = data.get("thermal_pressure")
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "samplers": list(samplers),
        "thermal_pressure": pressure if isinstance(pressure, str) else None,
        "cpu_die_c": _number(smc.get("cpu_die")),
        "gpu_die_c": _number(smc.get("gpu_die")),
        "fan_rpm": int(fan) if fan is not None else None,
        "cpu_power_w": _watts(processor.get("cpu_power")),
        "gpu_power_w": _watts(processor.get("gpu_power", gpu.get("gpu_power"))),
    }


def collect(runner=subprocess.run) -> dict[str, Any] | None:
    for samplers in SAMPLER_SETS:
        data = run_powermetrics(samplers, runner)
        if data is not None:
            return reduce_sample(data, samplers)
    return None


def append_line(path: str, record: dict[str, Any], keep: int = KEEP_LINES) -> None:
    """Append one record, keep the newest `keep`, and leave the file world-readable."""
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        pass
    lines = (lines + [json.dumps(record, sort_keys=True)])[-keep:]
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".powermetrics-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)  # the user-run sampler reads it
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str]) -> int:
    output = argv[1] if len(argv) > 1 else DEFAULT_OUTPUT
    if os.geteuid() != 0:
        print("powermetrics needs root; run this from the LaunchDaemon", file=sys.stderr)
        return 1
    record = collect()
    if record is None:
        print("powermetrics returned no usable sample", file=sys.stderr)
        return 1
    append_line(output, record)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
