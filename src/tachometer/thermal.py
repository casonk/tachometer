"""Frequent host temperature samples and the page that charts them.

The weekly host snapshot answers "what does this machine look like". It cannot
answer "was it overheating at 4 a.m.", because one reading a week is not a
history. This module takes a small sample every few minutes (CPU, GPU, board,
drives, fans, load), keeps a rolling window of them in a JSON-lines file, and
renders that window as a time series with a stoplight for the latest reading.

On Linux everything is read from sysfs and, for the GPU, `nvidia-smi`; nothing
here needs root. SATA drive temperatures appear only when the kernel's
`drivetemp` module is loaded. On macOS the unprivileged sources are the battery
temperature (`ioreg`, charted as the board reading) and each physical disk's
SMART verdict (`diskutil`); CPU temperature there needs root and is left out.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import limiters as rev_limiters
from .profile import _read_cpu_temp

SAMPLES_NAME = "host-samples.jsonl"
HOURLY_NAME = "host-samples-hourly.jsonl"
# Full five-minute samples are kept for a month. Older ones are folded into
# one record per hour (minimum, mean, maximum) and kept for a quarter.
DEFAULT_KEEP_DAYS = 30
DEFAULT_AGGREGATE_DAYS = 90
RANGES = {"6h": 6, "24h": 24, "7d": 168, "30d": 720, "90d": 2160}
# Ranges long enough that five-minute points stop being readable are drawn
# from hourly buckets instead.
HOURLY_RANGES = ("30d", "90d")
_FIELDS = (
    "cpu", "gpu", "board", "drive_max", "load", "fan_max", "gpu_w", "gpu_cap_w", "gpu_fan",
)  # fmt: skip
# Per-fan RPM is stored as sample["fans"] = {key: rpm} and flattened to "fan:<key>".
_FAN_PREFIX = "fan:"
# Per-disk activity is stored as sample["disks"] = {dev: {busy, read_mbs, write_mbs}} and
# flattened to "disk_busy:<dev>" (percent of the interval busy) and "disk_mbs:<dev>"
# (read plus write, megabytes per second).
_DISK_BUSY = "disk_busy:"
_DISK_MBS = "disk_mbs:"
_DYNAMIC = (_FAN_PREFIX, _DISK_BUSY, _DISK_MBS)
_WHOLE_DISK = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")
# Optional display names: TACHOMETER_FAN_LABELS="fan1=Rear exhaust,fan2=CPU,fan3=Front intake"
FAN_LABELS_ENV = "TACHOMETER_FAN_LABELS"

# (yellow at or above, red at or above), Celsius. Generic desktop guidance:
# yellow is "worth a look if sustained", red sits a little under the point
# where parts throttle or exceed their rated operating range.
LIMITS = {
    "cpu": (80.0, 90.0),
    "gpu": (78.0, 85.0),
    "board": (55.0, 65.0),
    "nvme": (70.0, 80.0),
    "disk": (50.0, 60.0),
}
_LIGHT_WORD = {"green": "OK", "yellow": "Watch", "red": "Hot", "unknown": "No data"}
_LIGHT_ICON = {"green": "✓", "yellow": "▲", "red": "✖", "unknown": "–"}
_LIGHT_RANK = {"unknown": 0, "green": 1, "yellow": 2, "red": 3}


def samples_path(tachometer_root: Path) -> Path:
    return Path(tachometer_root) / ".tachometer" / SAMPLES_NAME


def light_for(kind: str, value: float | None) -> str:
    if value is None:
        return "unknown"
    yellow_at, red_at = LIMITS[kind]
    return "red" if value >= red_at else "yellow" if value >= yellow_at else "green"


def _milli(path: Path) -> float | None:
    with contextlib.suppress(Exception):
        return int(path.read_text(encoding="utf-8").strip()) / 1000.0
    return None


def _text(path: Path) -> str:
    with contextlib.suppress(Exception):
        return path.read_text(encoding="utf-8").strip()
    return ""


def _read_board_temp(sys_class: Path) -> float | None:
    """Motherboard ambient: the Super I/O chip's SYSTIN, else the ACPI zone."""
    for chip in sorted((sys_class / "hwmon").glob("hwmon*")):
        if not _text(chip / "name").startswith(("nct", "it87", "f718")):
            continue
        for label in sorted(chip.glob("temp*_label")):
            if _text(label).upper() in ("SYSTIN", "SYSTEM", "MB TEMP"):
                temp = _milli(label.with_name(label.name.replace("_label", "_input")))
                if temp is not None:
                    return temp
    for zone in sorted((sys_class / "thermal").glob("thermal_zone*")):
        if _text(zone / "type") == "acpitz":
            return _milli(zone / "temp")
    return None


def _read_drive_temps(sys_class: Path) -> dict[str, float]:
    """Temperature per drive, keyed `nvme:<device>` or `disk:<device>`."""
    drives: dict[str, float] = {}
    for chip in sorted((sys_class / "hwmon").glob("hwmon*")):
        name = _text(chip / "name")
        if name not in ("nvme", "drivetemp"):
            continue
        temp = _milli(chip / "temp1_input")
        if temp is None:
            continue
        device = chip.name
        with contextlib.suppress(Exception):
            device = (chip / "device").resolve().name
        drives[f"{'nvme' if name == 'nvme' else 'disk'}:{device}"] = temp
    return drives


def _read_fan_rpm_max(sys_class: Path) -> int | None:
    best: int | None = None
    for fan in (sys_class / "hwmon").glob("hwmon*/fan*_input"):
        with contextlib.suppress(Exception):
            rpm = int(fan.read_text(encoding="utf-8").strip())
            best = rpm if best is None or rpm > best else best
    return best


def _read_fans(sys_class: Path) -> dict[str, int]:
    """RPM of every spinning fan, keyed `fanN` (`<chip>/fanN` when several chips report fans)."""
    chips = [
        chip
        for chip in sorted((sys_class / "hwmon").glob("hwmon*"))
        if any(chip.glob("fan*_input"))
    ]
    fans: dict[str, int] = {}
    for chip in chips:
        prefix = f"{_text(chip / 'name') or chip.name}/" if len(chips) > 1 else ""
        for path in chip.glob("fan*_input"):
            with contextlib.suppress(Exception):
                rpm = int(path.read_text(encoding="utf-8").strip())
                if rpm > 0:  # unconnected headers report 0
                    fans[prefix + path.name.removesuffix("_input")] = rpm
    return dict(sorted(fans.items(), key=lambda item: _natural(item[0])))


def _read_disk_counters(diskstats: Path = Path("/proc/diskstats")) -> dict[str, list[int]]:
    """Cumulative [sectors read, sectors written, ms busy] per whole disk."""
    counters: dict[str, list[int]] = {}
    with contextlib.suppress(OSError):
        for line in diskstats.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 13 and _WHOLE_DISK.match(fields[2]):
                with contextlib.suppress(ValueError):
                    counters[fields[2]] = [int(fields[5]), int(fields[9]), int(fields[12])]
    return counters


def disk_rates(previous: dict[str, Any] | None, sample: dict[str, Any]) -> dict[str, dict]:
    """Busy percent and MB/s per disk between two samples' counters.

    A disk whose counters went backwards (reboot, re-plug) is left out of
    that interval rather than reported as a huge negative rate.
    """
    if not previous or not previous.get("disk_counters") or not sample.get("disk_counters"):
        return {}
    seconds = sample["ts"] - previous["ts"]
    if not 0 < seconds <= 3600:
        return {}
    rates = {}
    for dev, (read, written, busy_ms) in sample["disk_counters"].items():
        before = previous["disk_counters"].get(dev)
        if not before:
            continue
        deltas = [read - before[0], written - before[1], busy_ms - before[2]]
        if min(deltas) < 0:
            continue
        rates[dev] = {
            "busy": round(min(100.0, deltas[2] / (seconds * 10)), 1),
            "read_mbs": round(deltas[0] * 512 / seconds / 1e6, 2),
            "write_mbs": round(deltas[1] * 512 / seconds / 1e6, 2),
        }
    return rates


def disk_labels(devs: list[str], sys_block: Path = Path("/sys/block")) -> dict[str, str]:
    """ "sdb · ST16000NT001 · 16 TB HDD" style names from sysfs."""
    labels = {}
    for dev in devs:
        base = sys_block / dev
        model = " ".join(_text(base / "device" / "model").split())
        size = ""
        with contextlib.suppress(ValueError):
            terabytes = int(_text(base / "size")) * 512 / 1e12
            size = f"{terabytes:.0f} TB" if terabytes >= 1 else f"{terabytes * 1000:.0f} GB"
        kind = (
            "NVMe"
            if dev.startswith("nvme")
            else ("HDD" if _text(base / "queue" / "rotational") == "1" else "SSD")
        )
        labels[dev] = " · ".join(part for part in (dev, model, f"{size} {kind}".strip()) if part)
    return labels


def _natural(key: str) -> tuple[str, int]:
    head = key.rstrip("0123456789")
    return head, int(key[len(head) :] or 0)


def fan_labels(keys: list[str], sys_class: Path = Path("/sys/class")) -> dict[str, str]:
    """Display names: TACHOMETER_FAN_LABELS first, then hwmon fanN_label, then "Fan N"."""
    configured = {}
    for pair in os.environ.get(FAN_LABELS_ENV, "").split(","):
        name, _, label = pair.partition("=")
        if name.strip() and label.strip():
            configured[name.strip()] = label.strip()
    labels = {}
    for key in keys:
        fan = key.rsplit("/", 1)[-1]
        if key in configured or fan in configured:
            labels[key] = configured.get(key) or configured[fan]
            continue
        hw = ""
        for chip in (sys_class / "hwmon").glob("hwmon*"):
            if "/" in key and _text(chip / "name") != key.split("/", 1)[0]:
                continue
            hw = _text(chip / f"{fan}_label") or hw
        labels[key] = hw or f"Fan {fan.removeprefix('fan')}"
    return labels


def _read_gpu_fan() -> float | None:
    tool = shutil.which("nvidia-smi")
    if not tool:
        return None
    with contextlib.suppress(Exception):
        out = subprocess.run(
            [tool, "--query-gpu=fan.speed", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        speeds = [float(v) for v in out.stdout.split() if v.replace(".", "", 1).isdigit()]
        if out.returncode == 0 and speeds:
            return max(speeds)
    return None


def _read_gpu_temp() -> float | None:
    tool = shutil.which("nvidia-smi")
    if not tool:
        return None
    with contextlib.suppress(Exception):
        out = subprocess.run(
            [tool, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        temps = [float(v) for v in out.stdout.split()]
        if out.returncode == 0 and temps:
            return max(temps)
    return None


def _run_plist(args: list[str]) -> Any:
    """Parsed plist output of a macOS tool, or None if it fails."""
    with contextlib.suppress(Exception):
        out = subprocess.run(args, capture_output=True, timeout=10, check=False)
        if out.returncode == 0 and out.stdout:
            return plistlib.loads(out.stdout)
    return None


def _read_macos_battery_temp(run=_run_plist) -> float | None:
    """Battery temperature in Celsius. `ioreg` reports hundredths of a degree.

    It is the one enclosure temperature macOS can expose without root, so it
    stands in for the board reading on a laptop. Recent Apple Silicon
    laptops leave the key out of `ioreg` entirely; the reading is then None.
    """
    entries = run(["ioreg", "-r", "-n", "AppleSmartBattery", "-a"])
    for entry in entries if isinstance(entries, list) else []:
        value = entry.get("Temperature") if isinstance(entry, dict) else None
        if isinstance(value, int) and value > 0:
            return round(value / 100.0, 1)
    return None


def _read_macos_smart(run=_run_plist) -> dict[str, dict[str, Any]]:
    """SMART verdict per physical disk, shaped like `read_smart` entries.

    `diskutil` gives only Verified or Failing, with no counters or
    temperature, so `rose` stays empty and `temp` is None.
    """
    listing = run(["diskutil", "list", "-plist", "physical"])
    disks = listing.get("WholeDisks", []) if isinstance(listing, dict) else []
    drives: dict[str, dict[str, Any]] = {}
    for disk in disks:
        info = run(["diskutil", "info", "-plist", str(disk)])
        status = info.get("SMARTStatus") if isinstance(info, dict) else None
        if status not in ("Verified", "Failing"):
            continue
        drives[str(disk)] = {
            "kind": "nvme" if info.get("SolidState") else "disk",
            "temp": None,
            "health_passed": status == "Verified",
            "rose": [],
            "age_seconds": 0,
        }
    return drives


# TACHOMETER_MACOS_POWERMETRICS names the JSON-lines file the root-run
# collector (scripts/macos/powermetrics_collector.py) appends to. It supplies
# what macOS hides from an unprivileged process: CPU and GPU die temperature
# and fan speed on Intel Macs, thermal pressure on every Mac.
MACOS_POWERMETRICS_ENV = "TACHOMETER_MACOS_POWERMETRICS"
# The collector runs every five minutes; older than this, it has stopped.
POWERMETRICS_MAX_AGE_SECONDS = 15 * 60
# macOS thermal pressure levels, as powermetrics names them, by severity.
_PRESSURE_LIGHT = {
    "nominal": "green",
    "fair": "yellow",
    "moderate": "yellow",
    "serious": "red",
    "heavy": "red",
    "critical": "red",
    "trapping": "red",
    "sleeping": "red",
}


def read_powermetrics(path: Path, *, now: float) -> dict[str, Any]:
    """The collector's newest reading, or {} when it is missing or stale."""
    history = _last_lines(path, count=1)
    if not history:
        return {}
    latest = history[-1]
    taken = _snapshot_epoch(latest)
    if taken is None or now - taken > POWERMETRICS_MAX_AGE_SECONDS:
        return {}
    return latest


def pressure_light(level: str | None) -> str:
    return _PRESSURE_LIGHT.get((level or "").lower(), "unknown")


# TACHOMETER_SMART_DIR names a directory holding one JSON-lines SMART history
# per drive, written by a separate root-run collector. This module only ever
# reads it; SMART itself needs root and the sampler deliberately has none.
# There is no default: unset means no SMART data.
SMART_DIR_ENV = "TACHOMETER_SMART_DIR"
# Collectors run hourly. Past this age a snapshot says nothing about now.
SMART_MAX_AGE_SECONDS = 3 * 3600


def _smart_dir() -> Path | None:
    value = os.environ.get(SMART_DIR_ENV, "").strip()
    return Path(value) if value else None


def _last_lines(path: Path, count: int = 2) -> list[dict[str, Any]]:
    """The last `count` JSON objects in a JSON-lines file, oldest first."""
    records: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 16384))
            tail = handle.read().decode("utf-8", errors="replace")
        for line in tail.splitlines()[-count:]:
            with contextlib.suppress(Exception):
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
    return records


def _snapshot_epoch(snapshot: dict[str, Any]) -> float | None:
    with contextlib.suppress(Exception):
        return datetime.fromisoformat(str(snapshot["taken_at"])).timestamp()
    return None


def read_smart(smart_dir: Path, *, now: float) -> dict[str, dict[str, Any]]:
    """Latest fresh SMART snapshot per drive, reduced to what the page needs.

    `rose` lists fault counters that increased since the previous snapshot of
    the same physical drive. A drive whose newest snapshot is stale is left
    out entirely rather than shown with old numbers.
    """
    drives: dict[str, dict[str, Any]] = {}
    for path in sorted(smart_dir.glob("*.jsonl")) if smart_dir.is_dir() else []:
        history = _last_lines(path)
        if not history:
            continue
        latest = history[-1]
        taken = _snapshot_epoch(latest)
        if taken is None or now - taken > SMART_MAX_AGE_SECONDS:
            continue
        rose: list[str] = []
        if len(history) == 2 and history[0].get("serial") == latest.get("serial"):
            before = history[0].get("counters") or {}
            for name, value in sorted((latest.get("counters") or {}).items()):
                earlier = before.get(name)
                if isinstance(value, int) and isinstance(earlier, int) and value > earlier:
                    rose.append(name)
        temp = latest.get("temperature_c")
        drives[path.stem] = {
            "kind": "nvme" if latest.get("kind") == "nvme" else "disk",
            "temp": float(temp) if isinstance(temp, int | float) else None,
            "health_passed": latest.get("health_passed"),
            "rose": rose,
            "age_seconds": int(now - taken),
        }
    return drives


def drive_health(smart: dict[str, dict[str, Any]]) -> dict[str, str]:
    """One light for drive health, with the reason in words."""
    if not smart:
        return {"light": "unknown", "detail": "No SMART data"}
    failed = sorted(name for name, d in smart.items() if d.get("health_passed") is False)
    if failed:
        return {"light": "red", "detail": "SMART failed: " + ", ".join(failed)}
    rising = sorted(name for name, d in smart.items() if d.get("rose"))
    if rising:
        return {"light": "yellow", "detail": "Fault counter rose: " + ", ".join(rising)}
    return {"light": "green", "detail": f"{len(smart)} drive(s) healthy"}


def collect_sample(
    sys_class: Path = Path("/sys/class"),
    *,
    now: float | None = None,
    gpu_reader=_read_gpu_temp,
    smart_dir: Path | None = None,
    platform: str | None = None,
    macos_run=_run_plist,
    limits_reader=None,
    gpu_fan_reader=_read_gpu_fan,
) -> dict[str, Any]:
    """One reading of everything the page charts. Missing sensors are None."""
    stamp = now if now is not None else time.time()
    macos = (platform or sys.platform) == "darwin"
    load = None
    with contextlib.suppress(Exception):
        load = round(os.getloadavg()[0] / (os.cpu_count() or 1), 3)
    drives = _read_drive_temps(sys_class)
    if smart_dir is None:
        smart_dir = _smart_dir()
    smart = read_smart(smart_dir, now=stamp) if smart_dir is not None else {}
    if macos and not smart:
        smart = _read_macos_smart(macos_run)
    power: dict[str, Any] = {}
    power_file = os.environ.get(MACOS_POWERMETRICS_ENV, "").strip()
    if macos and power_file:
        power = read_powermetrics(Path(power_file), now=stamp)
    for name, drive in smart.items():
        # SMART temperatures sit beside the kernel's, under their own prefix,
        # so the hottest reading wins whichever source it came from.
        if drive["temp"] is not None:
            drives[f"smart-{drive['kind']}:{name}"] = drive["temp"]
    sample = {
        "smart": smart,
        "ts": int(stamp),
        "load": load,
        "cpu": _read_cpu_temp(sys_class),
        "gpu": gpu_reader(),
        "board": _read_macos_battery_temp(macos_run) if macos else _read_board_temp(sys_class),
        "drive_max": max(drives.values()) if drives else None,
        "drives": drives,
        "fan_max": _read_fan_rpm_max(sys_class),
        "fans": {} if macos else _read_fans(sys_class),
        "disk_counters": {} if macos else _read_disk_counters(),
        "gpu_fan": None if macos else gpu_fan_reader(),
    }
    caps: dict[str, Any] = {}
    if not macos:
        caps = (limits_reader or (lambda: rev_limiters.read_limiters(sys_class)))()
    gpus = caps.get("gpu") or []
    draws = [g["power_draw_w"] for g in gpus if g.get("power_draw_w") is not None]
    limits = [g["power_limit_w"] for g in gpus if g.get("power_limit_w") is not None]
    sample["gpu_w"] = round(sum(draws), 1) if draws else None
    sample["gpu_cap_w"] = round(sum(limits), 1) if limits else None
    if caps:
        sample["limiters"] = caps
    if power:
        for field, key in (("cpu", "cpu_die_c"), ("gpu", "gpu_die_c"), ("fan_max", "fan_rpm")):
            if sample[field] is None and isinstance(power.get(key), int | float):
                sample[field] = power[key]
        sample["pressure"] = power.get("thermal_pressure")
        sample["power_w"] = {
            "cpu": power.get("cpu_power_w"),
            "gpu": power.get("gpu_power_w"),
        }
    return sample


def load_samples(path: Path, *, since: float | None = None) -> list[dict[str, Any]]:
    """Samples in file order, optionally only those at or after `since`."""
    samples: list[dict[str, Any]] = []
    with contextlib.suppress(FileNotFoundError):
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(Exception):
                sample = json.loads(line)
                if (
                    isinstance(sample, dict)
                    and isinstance(sample.get("ts"), int | float)
                    and (since is None or sample["ts"] >= since)
                ):
                    samples.append(sample)
    return samples


def hourly_path(tachometer_root: Path) -> Path:
    return Path(tachometer_root) / ".tachometer" / HOURLY_NAME


def _fold(bucket: dict[str, Any], sample: dict[str, Any]) -> None:
    """Merge one raw sample into an hourly bucket, field by field."""
    bucket["n"] = bucket.get("n", 0) + 1
    for name, value in _chartable(sample).items():
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        stat = bucket.setdefault(name, {"min": value, "max": value, "sum": 0.0, "n": 0})
        stat["min"] = min(stat["min"], value)
        stat["max"] = max(stat["max"], value)
        stat["sum"] = round(stat["sum"] + value, 3)
        stat["n"] += 1


def _chartable(sample: dict[str, Any]) -> dict[str, Any]:
    """The sample's numeric series, per-fan RPM flattened to "fan:<key>"."""
    values = {name: sample.get(name) for name in _FIELDS}
    for key, rpm in (sample.get("fans") or {}).items():
        values[_FAN_PREFIX + key] = rpm
    for dev, rate in (sample.get("disks") or {}).items():
        values[_DISK_BUSY + dev] = rate.get("busy")
        values[_DISK_MBS + dev] = round(
            (rate.get("read_mbs") or 0) + (rate.get("write_mbs") or 0), 2
        )
    for key, value in sample.items():
        if key.startswith(_DYNAMIC):  # already flattened (an hourly point)
            values[key] = value
    return values


def _hour(ts: float) -> int:
    return int(ts) - int(ts) % 3600


def fold_into_hourly(
    buckets: dict[int, dict[str, Any]], samples: list[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    for sample in samples:
        _fold(buckets.setdefault(_hour(sample["ts"]), {"ts": _hour(sample["ts"])}), sample)
    return buckets


def load_hourly(path: Path) -> dict[int, dict[str, Any]]:
    return {int(b["ts"]): b for b in load_samples(path)}


def _write_lines(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")
    os.replace(tmp, path)


def append_sample(
    path: Path,
    sample: dict[str, Any],
    *,
    keep_days: float = DEFAULT_KEEP_DAYS,
    aggregate_days: float = DEFAULT_AGGREGATE_DAYS,
) -> int:
    """Append one sample; age older samples into hourly records.

    Samples older than `keep_days` leave the raw file and are folded into
    `host-samples-hourly.jsonl`, whose records are dropped after
    `aggregate_days`. The hourly file is written first, so a crash between the
    two writes can at worst count a few samples twice in one hour, never lose
    them. Returns the number of raw samples kept.
    """
    cutoff = sample["ts"] - keep_days * 86400
    existing = load_samples(path)
    if "disks" not in sample:
        sample["disks"] = disk_rates(existing[-1] if existing else None, sample)
    expired = [s for s in existing if s["ts"] < cutoff]
    kept = [s for s in existing if s["ts"] >= cutoff]
    kept.append(sample)

    hourly_file = path.with_name(HOURLY_NAME)
    if expired or hourly_file.exists():
        buckets = fold_into_hourly(load_hourly(hourly_file), expired)
        oldest = sample["ts"] - aggregate_days * 86400
        pruned = [b for ts, b in sorted(buckets.items()) if ts >= _hour(oldest)]
        if expired or len(pruned) != len(buckets):
            _write_lines(hourly_file, pruned)
    _write_lines(path, kept)
    return len(kept)


def hourly_series(
    raw: list[dict[str, Any]], stored: dict[int, dict[str, Any]], *, since: float
) -> list[dict[str, Any]]:
    """One point per hour from stored hourly records plus the raw window.

    Temperatures and fan speed report the hour's maximum, because a peak is
    what matters for heat and an average hides it. Load reports the mean.
    """
    buckets = {ts: json.loads(json.dumps(b)) for ts, b in stored.items()}
    fold_into_hourly(buckets, raw)
    points = []
    for ts, bucket in sorted(buckets.items()):
        if ts < _hour(since):
            continue
        point: dict[str, Any] = {"ts": ts}
        names = list(_FIELDS) + sorted(k for k in bucket if k.startswith(_DYNAMIC))
        for name in names:
            stat = bucket.get(name)
            if not stat or not stat.get("n"):
                point[name] = None
            elif name == "load":
                point[name] = round(stat["sum"] / stat["n"], 3)
            else:
                point[name] = stat["max"]
        points.append(point)
    return points


def drive_light(drives: dict[str, float]) -> str:
    """Worst light across drives, each judged against the limit for its kind."""
    worst = "unknown"
    for key, temp in drives.items():
        is_nvme = key.startswith(("nvme:", "smart-nvme:"))
        light = light_for("nvme" if is_nvme else "disk", temp)
        if _LIGHT_RANK[light] > _LIGHT_RANK[worst]:
            worst = light
    return worst


def latest_status(samples: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    """The newest sample reduced to labelled readings, each with a light."""
    if not samples:
        return {"has_data": False, "readings": [], "overall": "unknown", "age_seconds": None}
    last = samples[-1]
    readings = [
        {"label": "CPU", "value": last.get("cpu"), "light": light_for("cpu", last.get("cpu"))},
        {"label": "GPU", "value": last.get("gpu"), "light": light_for("gpu", last.get("gpu"))},
        {
            "label": "Board",
            "value": last.get("board"),
            "light": light_for("board", last.get("board")),
        },
        {
            "label": "Hottest drive",
            "value": last.get("drive_max"),
            "light": drive_light(last.get("drives") or {}),
        },
    ]
    health = drive_health(last.get("smart") or {})
    readings.append(
        {"label": "Drive health", "value": None, "light": health["light"], "text": health["detail"]}
    )
    if last.get("pressure"):
        # Only macOS with the root collector reports this, so other hosts keep
        # the same five readings they always had.
        readings.append(
            {
                "label": "Thermal pressure",
                "value": None,
                "light": pressure_light(last["pressure"]),
                "text": str(last["pressure"]),
                "verdict": {"green": "OK", "yellow": "Watch", "red": "Throttling"}.get(
                    pressure_light(last["pressure"]), "No data"
                ),
            }
        )
    overall = max((r["light"] for r in readings), key=_LIGHT_RANK.__getitem__)
    return {
        "has_data": True,
        "readings": readings,
        "overall": overall,
        "age_seconds": int((now if now is not None else time.time()) - last["ts"]),
        "load": last.get("load"),
        "fan_max": last.get("fan_max"),
        "limiters": last.get("limiters") or {},
        "limiter_rows": rev_limiters.describe(last.get("limiters") or {}),
    }


def build_payload(
    tachometer_root: Path, range_key: str = "24h", *, now: float | None = None
) -> dict:
    range_key = range_key if range_key in RANGES else "24h"
    now = now if now is not None else time.time()
    since = now - RANGES[range_key] * 3600
    all_samples = load_samples(samples_path(tachometer_root))
    if range_key in HOURLY_RANGES:
        resolution = "hourly"
        points = hourly_series(all_samples, load_hourly(hourly_path(tachometer_root)), since=since)
    else:
        resolution = "5min"
        points = [{"ts": s["ts"], **_chartable(s)} for s in all_samples if s["ts"] >= since]
    fan_keys = sorted(
        {k.removeprefix(_FAN_PREFIX) for p in points for k in p if k.startswith(_FAN_PREFIX)},
        key=_natural,
    )
    disk_devs = sorted(
        {k.removeprefix(_DISK_BUSY) for p in points for k in p if k.startswith(_DISK_BUSY)}
    )
    return {
        "disk_labels": disk_labels(disk_devs),
        "fan_labels": fan_labels(fan_keys),
        "range": range_key,
        "resolution": resolution,
        "limits": LIMITS,
        "status": latest_status(all_samples, now=now),
        "samples": points,
    }


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tachometer · Thermal</title>
<style>
:root{--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--rule:#e6e5e1;
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#8a5cd6;--s6:#d6409f;
 --good:#0ca30c;--warning:#b97b00;--critical:#d03b3b;--none:#8a8983}
@media (prefers-color-scheme:dark){:root{--plane:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
 --ink2:#c3c2b7;--rule:#32322f;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#a07ae8;--s6:#e05aab;
 --warning:#fab219;--none:#8a8983}}
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 40px;background:var(--plane);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
main{max-width:1000px;margin:0 auto}
h1{font-size:1.25rem;margin:0 0 2px}
h2{font-size:.95rem;margin:0 0 2px}
.sub{color:var(--ink2);font-size:.8rem;margin-bottom:14px}
a{color:inherit}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.chip{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
 padding:8px 12px;min-width:118px}
.chip .k{color:var(--ink2);font-size:.72rem}
.chip .v{font-size:1.25rem;font-weight:600;font-variant-numeric:tabular-nums}
.chip .s{font-size:.75rem;display:flex;align-items:center;gap:5px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.green{background:var(--good)}.yellow{background:var(--warning)}
.red{background:var(--critical)}.unknown{background:var(--none)}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:12px;
 padding:14px 14px 10px;margin-bottom:14px;position:relative}
.filters{display:flex;gap:6px;margin-bottom:12px}
.filters a{padding:4px 12px;border:1px solid var(--rule);border-radius:999px;
 text-decoration:none;font-size:.8rem;background:var(--surface)}
.filters a[aria-current=true]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:.78rem;color:var(--ink2);margin:6px 0 2px}
.legend i{display:inline-block;width:14px;height:2px;border-radius:1px;vertical-align:middle;margin-right:5px}
svg{display:block;width:100%;height:auto;overflow:visible}
.tick{fill:var(--ink2);font-size:11px;font-variant-numeric:tabular-nums}
.end{fill:var(--ink);font-size:11px;font-variant-numeric:tabular-nums}
.grid{stroke:var(--rule);stroke-width:1}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--rule);
 border-radius:8px;padding:7px 10px;font-size:.76rem;box-shadow:0 4px 14px rgba(0,0,0,.14);
 white-space:nowrap;display:none;font-variant-numeric:tabular-nums}
.tip b{font-weight:600}
.empty{color:var(--ink2);padding:28px 0;text-align:center}
table{border-collapse:collapse;width:100%;font-size:.78rem;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:3px 8px;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{text-align:left}
.limits td,.limits th{text-align:left}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:end;margin-top:10px;font-size:.8rem}
.controls form{display:flex;gap:6px;align-items:end}
.controls label{display:flex;flex-direction:column;gap:3px;color:var(--ink2)}
.controls input,.controls select{font:inherit;padding:4px 6px;border:1px solid var(--rule);border-radius:6px;
 background:var(--plane);color:var(--ink)}
.controls input{width:6.5em}
.controls button{font:inherit;padding:4px 12px;border:1px solid var(--ink);border-radius:6px;
 background:var(--ink);color:var(--surface);cursor:pointer}
.note{font-size:.78rem;color:var(--ink2);margin-top:8px}
.msg{padding:8px 12px;border-radius:8px;margin-bottom:12px;font-size:.85rem;border:1px solid var(--rule)}
.msg.ok{border-color:var(--good)}.msg.err{border-color:var(--critical)}
summary{cursor:pointer;color:var(--ink2);font-size:.8rem}
</style></head><body><main>
<h1>Thermal history</h1>
<div class="sub"><a href="/">&larr; Dashboard</a> &middot; __AGE__ &middot;
 <a href="/api/thermal?range=__RANGE__">JSON</a></div>
__MESSAGE__<div class="chips">__CHIPS__</div>
<div class="filters" role="navigation" aria-label="Time range">__FILTERS__</div>
<div class="card"><h2>Temperature</h2>
 <div class="sub" style="margin:0">Degrees Celsius, __TEMP_NOTE__</div>
 <div class="legend" id="legend"></div><div id="temp"></div><div class="tip" id="tip-temp"></div></div>
__LIMITERS__
__BENCH__
<div class="card" id="power-card"><h2>GPU power</h2>
 <div class="sub" style="margin:0">Watts drawn against the power cap (the rev limiter)__POWER_NOTE__</div>
 <div id="power"></div><div class="tip" id="tip-power"></div></div>
<div class="card" id="fans-card"><h2>Fans</h2>
 <div class="sub" style="margin:0">Revolutions per minute for each spinning fan__FAN_NOTE__</div>
 <div class="legend" id="fan-legend"></div><div id="fans"></div><div class="tip" id="tip-fans"></div>
 <div id="gpufan-wrap"><div class="sub" style="margin:10px 0 0">GPU fan, percent of its maximum</div>
 <div id="gpufan"></div><div class="tip" id="tip-gpufan"></div></div></div>
<div class="card" id="disks-card"><h2>Disks</h2>
 <div class="sub" style="margin:0">Percent of each interval the disk was busy__DISK_NOTE__</div>
 <div class="legend" id="disk-legend"></div><div id="diskbusy"></div><div class="tip" id="tip-diskbusy"></div>
 <div class="sub" style="margin:10px 0 0">Throughput, read plus write, megabytes per second</div>
 <div id="diskmbs"></div><div class="tip" id="tip-diskmbs"></div></div>
<div class="card"><h2>Load per CPU thread</h2>
 <div class="sub" style="margin:0">1-minute load average divided by thread count; 1.0 is fully busy__LOAD_NOTE__</div>
 <div id="load"></div><div class="tip" id="tip-load"></div></div>
<details class="card"><summary>Table view (__COUNT__ __UNIT__)</summary><div id="table"></div></details>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),S=D.samples;
const SERIES=[['cpu','CPU','--s1'],['gpu','GPU','--s2'],['board','Board','--s3'],['drive_max','Hottest drive','--s4']];
const NS='http://www.w3.org/2000/svg';
const el=(n,a,t)=>{const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);if(t!=null)e.textContent=t;return e};
const fmtT=ts=>{const d=new Date(ts*1000);return d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})};
const fmtX=ts=>{const d=new Date(ts*1000);return D.resolution==='hourly'?d.toLocaleDateString([], {month:'short',day:'numeric'}):D.range==='7d'?d.toLocaleDateString([], {weekday:'short',day:'numeric'}):d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})};
function chart(host,tipId,series,opts){
  const box=document.getElementById(host),tip=document.getElementById(tipId);
  const have=series.filter(s=>S.some(p=>p[s[0]]!=null));
  if(S.length<2||!have.length){box.innerHTML='<div class="empty">Not enough samples in this range yet.</div>';return}
  const W=960,H=opts.h,L=38,R=opts.endLabels?118:14,T=10,B=24;
  const t0=S[0].ts,t1=S[S.length-1].ts;
  let lo=Infinity,hi=-Infinity;
  S.forEach(p=>have.forEach(s=>{const v=p[s[0]];if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v)}}));
  lo=opts.zero?0:Math.floor(lo/10)*10;hi=Math.max(opts.min||0,Math.ceil(hi/opts.step)*opts.step);
  if(hi<=lo)hi=lo+opts.step;
  const x=t=>L+(t-t0)/(t1-t0||1)*(W-L-R),y=v=>T+(1-(v-lo)/(hi-lo))*(H-T-B);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':opts.label});
  for(let v=lo;v<=hi+1e-9;v+=opts.step){
    svg.append(el('line',{class:'grid',x1:L,x2:W-R,y1:y(v),y2:y(v)}));
    svg.append(el('text',{class:'tick',x:L-6,y:y(v)+4,'text-anchor':'end'},opts.fmt(v)))}
  for(let i=0;i<=4;i++){const t=t0+(t1-t0)*i/4;
    svg.append(el('text',{class:'tick',x:x(t),y:H-6,'text-anchor':i===0?'start':i===4?'end':'middle'},fmtX(t)))}
  const css=getComputedStyle(document.documentElement);
  const ends=[];
  have.forEach(s=>{let d='',pen=false,last=null;
    S.forEach(p=>{const v=p[s[0]];if(v==null){pen=false;return}
      d+=(pen?'L':'M')+x(p.ts).toFixed(1)+' '+y(v).toFixed(1);pen=true;last=[p.ts,v]});
    svg.append(el('path',{d,fill:'none',stroke:css.getPropertyValue(s[2]),'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
    if(last)ends.push({y:y(last[1]),text:`${s[1]} ${opts.fmt(last[1])}`})});
  if(opts.endLabels){ends.sort((a,b)=>a.y-b.y);
    ends.forEach((e,i)=>{if(i&&e.y-ends[i-1].ly<13)e.ly=ends[i-1].ly+13;else e.ly=e.y;
      svg.append(el('text',{class:'end',x:W-R+6,y:e.ly+4},e.text))})}
  const cross=el('line',{class:'grid',y1:T,y2:H-B,style:'display:none;stroke:var(--ink2)'});svg.append(cross);
  const hit=el('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'});svg.append(hit);
  const move=ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)/r.width*W;
    const t=t0+(px-L)/(W-L-R)*(t1-t0);let best=S[0];
    for(const p of S)if(Math.abs(p.ts-t)<Math.abs(best.ts-t))best=p;
    cross.setAttribute('x1',x(best.ts));cross.setAttribute('x2',x(best.ts));cross.style.display='';
    tip.innerHTML='<b>'+fmtT(best.ts)+'</b><br>'+have.map(s=>`<span class="legend"><i style="background:var(${s[2]})"></i></span>${s[1]}: <b>${best[s[0]]==null?'–':opts.fmt(best[s[0]])}</b>`).join('<br>');
    tip.style.display='block';const cr=box.parentElement.getBoundingClientRect();
    let lx=ev.clientX-cr.left+14;if(lx+tip.offsetWidth>cr.width-8)lx=ev.clientX-cr.left-tip.offsetWidth-14;
    tip.style.left=lx+'px';tip.style.top=(ev.clientY-cr.top-10)+'px'};
  hit.addEventListener('pointermove',move);
  hit.addEventListener('pointerleave',()=>{tip.style.display='none';cross.style.display='none'});
  box.append(svg);return have}
const shown=chart('temp','tip-temp',SERIES,{h:300,step:10,min:60,endLabels:true,label:'Temperature over time',fmt:v=>Math.round(v)+'\\u00b0'})||[];
document.getElementById('legend').innerHTML=shown.map(s=>`<span><i style="background:var(${s[2]})"></i>${s[1]}</span>`).join('');
if(!chart('power','tip-power',[['gpu_w','Draw','--s2'],['gpu_cap_w','Cap','--s3']],{h:170,step:50,min:100,zero:true,endLabels:true,label:'GPU power draw and cap over time',fmt:v=>Math.round(v)+' W'}))document.getElementById('power-card').style.display='none';
const FANS=[...new Set(S.flatMap(p=>Object.keys(p).filter(k=>k.startsWith('fan:'))))]
  .map((k,i)=>[k,(D.fan_labels||{})[k.slice(4)]||k.slice(4),'--s'+(i%6+1)]);
// Per-fan readings start with this version; the fastest-fan line carries the older history.
const fanShown=chart('fans','tip-fans',[...FANS,['fan_max','Fastest fan','--s6']],{h:220,step:500,min:1000,zero:true,endLabels:true,label:'Fan speeds over time',fmt:v=>Math.round(v)+' rpm'});
document.getElementById('fan-legend').innerHTML=(fanShown||[]).map(s=>`<span><i style="background:var(${s[2]})"></i>${s[1]}</span>`).join('');
if(!chart('gpufan','tip-gpufan',[['gpu_fan','GPU fan','--s2']],{h:130,step:25,min:100,zero:true,endLabels:true,label:'GPU fan speed over time',fmt:v=>Math.round(v)+'%'}))document.getElementById('gpufan-wrap').style.display='none';
if(!fanShown&&document.getElementById('gpufan-wrap').style.display==='none')document.getElementById('fans-card').style.display='none';
const DEVS=[...new Set(S.flatMap(p=>Object.keys(p).filter(k=>k.startsWith('disk_busy:'))))].map(k=>k.slice(10)).sort();
const devName=d=>(D.disk_labels||{})[d]||d;
const busyShown=chart('diskbusy','tip-diskbusy',DEVS.map((d,i)=>['disk_busy:'+d,devName(d),'--s'+(i%6+1)]),{h:180,step:25,min:100,zero:true,endLabels:false,label:'Disk busy percent over time',fmt:v=>Math.round(v)+'%'});
chart('diskmbs','tip-diskmbs',DEVS.map((d,i)=>['disk_mbs:'+d,devName(d),'--s'+(i%6+1)]),{h:180,step:50,min:50,zero:true,endLabels:false,label:'Disk throughput over time',fmt:v=>(+v).toFixed(v<10?1:0)+' MB/s'});
document.getElementById('disk-legend').innerHTML=(busyShown||[]).map(s=>`<span><i style="background:var(${s[2]})"></i>${s[1]}</span>`).join('');
if(!busyShown)document.getElementById('disks-card').style.display='none';
chart('load','tip-load',[['load','Load','--s1']],{h:150,step:0.5,min:1,zero:true,endLabels:false,label:'Load per CPU thread over time',fmt:v=>(+v).toFixed(2)});
const rows=S.slice(-200).reverse().map(p=>'<tr><td>'+fmtT(p.ts)+'</td>'+['cpu','gpu','board','drive_max'].map(k=>'<td>'+(p[k]==null?'–':Math.round(p[k]))+'</td>').join('')+'<td>'+(p.load==null?'–':p.load.toFixed(2))+'</td><td>'+(p.fan_max==null?'–':p.fan_max)+'</td></tr>').join('');
document.getElementById('table').innerHTML='<table><thead><tr><th>Time</th><th>CPU °C</th><th>GPU °C</th><th>Board °C</th><th>Hottest drive °C</th><th>Load</th><th>Fan max RPM</th></tr></thead><tbody>'+rows+'</tbody></table>';
</script></main></body></html>
"""


def _message(message: str | None, ok: bool) -> str:
    if not message:
        return ""
    return f'<div class="msg {"ok" if ok else "err"}" role="status">{html.escape(message)}</div>'


def _controls_form(limiters: dict[str, Any], controls: dict[str, Any], range_key: str) -> str:
    """The "Set limiters" forms, or how to enable them when the helper is missing."""
    if not controls.get("available"):
        reason = controls.get("reason") or "unavailable"
        return (
            '<div class="note">Changing limiters here is off ('
            f"{html.escape(reason)}). Enable it with fedora-debugg's "
            "<code>sudo ./scripts/install_limiter_controls.sh --user $USER</code>.</div>"
        )
    forms = []
    hidden = f'<input type="hidden" name="range" value="{html.escape(range_key)}">'
    gpus = limiters.get("gpu") or []
    if gpus:
        gpu = gpus[0]
        low, high = gpu.get("min_limit_w") or 1, gpu.get("max_limit_w") or 2000
        current = gpu.get("power_limit_w") or gpu.get("default_limit_w") or high
        default = gpu.get("default_limit_w")
        forms.append(
            '<form method="post" action="/thermal/limiters">'
            f'{hidden}<input type="hidden" name="setting" value="gpu_power">'
            f'<label>GPU power cap (W)<input type="number" name="value" '
            f'min="{low:.0f}" max="{high:.0f}" step="5" value="{current:.0f}" required></label>'
            "<button>Set</button></form>"
            f'<span class="note" style="margin:0">{low:.0f}–{high:.0f} W'
            + (f", stock {default:.0f} W" if default else "")
            + "</span>"
        )
    profiles = controls.get("profiles") or []
    current_profile = (limiters.get("profile") or {}).get("name")
    if profiles:
        options = "".join(
            f"<option{' selected' if p == current_profile else ''}>{html.escape(p)}</option>"
            for p in profiles
        )
        forms.append(
            '<form method="post" action="/thermal/limiters">'
            f'{hidden}<input type="hidden" name="setting" value="profile">'
            f'<label>Power profile<select name="value">{options}</select></label>'
            "<button>Set</button></form>"
        )
    disks = _disk_forms(limiters.get("disks") or [], hidden)
    if not forms and not disks:
        return ""
    return (
        '<div class="controls">'
        + "".join(forms)
        + '</div><div class="note">Changes apply immediately and survive reboots '
        "(GPU cap through fedora-debugg's boot unit, profile through tuned, disks through "
        "a udev rule keyed by each drive's serial).</div>" + disks
    )


def _disk_forms(disks: list[dict[str, Any]], hidden: str) -> str:
    """Scheduler and read-ahead per disk, read-ahead per volume, each with a reset."""
    from .limiters import READ_AHEAD_CHOICES

    def read_ahead_select(current: int | None) -> str:
        choices = list(READ_AHEAD_CHOICES)
        if current is not None and current not in choices:
            choices = sorted([*choices, current])

        def label(kb: int) -> str:
            return f"{kb // 1024} MiB" if kb >= 1024 and kb % 1024 == 0 else f"{kb} KiB"

        return (
            '<label>Read-ahead<select name="read_ahead">'
            + "".join(
                f'<option value="{kb}"{" selected" if kb == current else ""}>{label(kb)}</option>'
                for kb in choices
            )
            + "</select></label>"
        )

    def row(dev: str, label: str, fields: str, was: str) -> str:
        target = f'<input type="hidden" name="dev" value="{html.escape(dev)}">'
        return (
            '<div class="controls">'
            f'<form method="post" action="/thermal/limiters">{hidden}{target}{was}'
            '<input type="hidden" name="setting" value="disk">'
            f'<span style="min-width:15em">{html.escape(label)}</span>{fields}'
            "<button>Set</button></form>"
            f'<form method="post" action="/thermal/limiters">{hidden}{target}'
            '<input type="hidden" name="setting" value="disk_reset">'
            '<button title="Back to the defaults for this drive type">Reset</button></form>'
            "</div>"
        )

    rows = []
    for disk in disks:
        current = disk.get("scheduler") or ""
        options = "".join(
            f"<option{' selected' if s == current else ''}>{html.escape(s)}</option>"
            for s in disk.get("schedulers") or [current]
            if s
        )
        was = (
            f'<input type="hidden" name="was_scheduler" value="{html.escape(current)}">'
            f'<input type="hidden" name="was_read_ahead" value="{disk.get("read_ahead_kb") or ""}">'
        )
        label = f"{disk['dev']}" + (f" {disk['model']}" if disk.get("model") else "")
        rows.append(
            row(
                disk["dev"],
                label,
                f'<label>Scheduler<select name="scheduler">{options}</select></label>'
                + read_ahead_select(disk.get("read_ahead_kb")),
                was,
            )
        )
        for volume in disk.get("volumes") or []:
            if not volume.get("dev"):
                continue
            was = (
                '<input type="hidden" name="was_scheduler" value="">'
                f'<input type="hidden" name="was_read_ahead" '
                f'value="{volume.get("read_ahead_kb") or ""}">'
            )
            rows.append(
                row(
                    volume["dev"],
                    f"\u2514 volume {volume['name']}",
                    read_ahead_select(volume.get("read_ahead_kb")),
                    was,
                )
            )
    if not rows:
        return ""
    return (
        '<details style="margin-top:10px"><summary>Disk I/O settings</summary>'
        + "".join(rows)
        + '<div class="note">Defaults by drive type come from fedora-debugg\'s udev rule; '
        "these override one device and follow it if it moves to another port. Larger "
        "read-ahead speeds long sequential reads and costs random access (see the tuning "
        "log).</div></details>"
    )


def _limiter_card(
    rows: list[dict[str, str]],
    limiters: dict[str, Any] | None = None,
    controls: dict[str, Any] | None = None,
    range_key: str = "24h",
) -> str:
    """Latest caps, one row each, with a dot when one is set or currently biting."""
    if not rows:
        return ""
    body = "".join(
        "<tr><td>"
        f'<span class="dot {"unknown" if row["light"] == "none" else row["light"]}"></span> '
        f"{html.escape(row['limiter'])}</td><td>{html.escape(row['setting'])}</td>"
        f"<td>{html.escape(row['state'])}</td></tr>"
        for row in rows
    )
    return (
        '<div class="card"><h2>Rev limiters</h2>'
        '<div class="sub" style="margin:0 0 6px">The caps this host runs under, from the latest '
        "sample, including ceilings that cannot be changed in software. A green dot is a "
        "limiter that is set, a disk tuned for its type or a link running at full width; amber "
        "means it is holding the hardware back right now: a cap biting, a disk scheduler that "
        "does not suit it, or a PCIe slot narrower or older than the device in it."
        "</div>"
        '<table class="limits"><thead><tr><th>Limiter</th><th>Setting</th><th>Now</th></tr>'
        f"</thead><tbody>{body}</tbody></table>"
        + _controls_form(limiters or {}, controls or {}, range_key)
        + "</div>"
    )


def _bench_card(tachometer_root: Path, limit: int = 12) -> str:
    """Recent nightly benchmark runs, newest first, with the headline numbers."""
    from . import bench

    records = bench.load_history(bench.history_path(tachometer_root))
    if not records:
        return (
            '<div class="card"><h2>Benchmarks</h2><div class="sub" style="margin:0">'
            "No runs yet. Weekly short, monthly mid and quarterly full tiers run at night "
            "once the tachometer-bench timer is installed.</div></div>"
        )

    def num(value: Any, unit: str, digits: int = 0) -> str:
        return "–" if value is None else f"{value:,.{digits}f} {unit}"

    rows = []
    for record in reversed(records[-limit:]):
        top = bench.headline(record)
        status = record.get("status", "?")
        why = record.get("reason") or (
            f"stopped at {record['stopped_by']}" if record.get("stopped_by") else ""
        )
        light = {"completed": "green", "aborted": "yellow", "skipped": "unknown"}.get(
            status, "unknown"
        )
        disks = (
            ", ".join(f"{html.escape(k)} {num(v, 'MB/s')}" for k, v in top["disks"].items()) or "–"
        )
        peaks = " / ".join(
            num(top[k], "°C") for k in ("peak_cpu_c", "peak_gpu_c") if top[k] is not None
        )
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("started_at", 0)))
        rows.append(
            f"<tr><td>{started}</td><td>{html.escape(str(record.get('tier')))}</td>"
            f'<td><span class="dot {light}"></span> {html.escape(status)}'
            f"{'<br><small>' + html.escape(why) + '</small>' if why else ''}</td>"
            f"<td>{disks}</td><td>{num(top['memory_copy_GBps'], 'GB/s', 1)}</td>"
            f"<td>{num(top['cpu_rating_mips'], 'MIPS')}</td>"
            f"<td>{num(top['gpu_tflops'], 'TFLOPS', 1)}</td><td>{peaks or '–'}</td></tr>"
        )
    return (
        '<div class="card"><h2>Benchmarks</h2>'
        '<div class="sub" style="margin:0 0 6px">Nightly runs, newest first: weekly short, '
        "monthly mid, quarterly full. Load stages stop at 78 °C CPU / 80 °C GPU; skipped "
        "nights say why.</div>"
        '<table class="limits"><thead><tr><th>Started</th><th>Tier</th><th>Result</th>'
        "<th>Cold read</th><th>Memory copy</th><th>CPU (7-Zip)</th><th>GPU FP16</th>"
        f"<th>Peak CPU / GPU</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "no samples yet"
    if seconds < 90:
        return "latest sample just now"
    if seconds < 5400:
        return f"latest sample {seconds // 60} min ago"
    return f"latest sample {seconds // 3600} h ago"


def render_page(
    tachometer_root: Path,
    range_key: str = "24h",
    *,
    now: float | None = None,
    message: str | None = None,
    message_ok: bool = True,
    controls: dict[str, Any] | None = None,
) -> str:
    payload = build_payload(tachometer_root, range_key, now=now)
    status = payload["status"]
    chips = []
    for reading in status["readings"]:
        light = reading["light"]
        if "text" in reading:
            # A status reading: the headline is the verdict, the detail says why.
            value = reading.get("verdict") or (_LIGHT_WORD[light] if light != "red" else "Failing")
            note = html.escape(reading["text"])
        else:
            value = "–" if reading["value"] is None else f"{reading['value']:.0f}°C"
            note = _LIGHT_WORD[light]
        chips.append(
            f'<div class="chip"><div class="k">{html.escape(reading["label"])}</div>'
            f'<div class="v">{value}</div><div class="s"><span class="dot {light}"></span>'
            f"{_LIGHT_ICON[light]} {note}</div></div>"
        )
    if not chips:
        chips.append(
            '<div class="chip"><div class="k">Host</div><div class="v">–</div>'
            '<div class="s">No samples yet</div></div>'
        )
    filters = "".join(
        f'<a href="/thermal?range={key}" aria-current="{"true" if key == payload["range"] else "false"}">'
        f"{key}</a>"
        for key in RANGES
    )
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    hourly = payload["resolution"] == "hourly"
    return (
        _PAGE.replace("__CHIPS__", "".join(chips))
        .replace("__FILTERS__", filters)
        .replace("__AGE__", _fmt_age(status["age_seconds"]))
        .replace("__RANGE__", payload["range"])
        .replace("__COUNT__", str(len(payload["samples"])))
        .replace("__UNIT__", "hours" if hourly else "samples")
        .replace(
            "__TEMP_NOTE__",
            "hourly maximum" if hourly else "one sample about every five minutes",
        )
        .replace("__LOAD_NOTE__", " (hourly mean)" if hourly else "")
        .replace("__POWER_NOTE__", "; hourly maximum" if hourly else "")
        .replace("__FAN_NOTE__", "; hourly maximum" if hourly else "")
        .replace("__DISK_NOTE__", "; hourly maximum" if hourly else "")
        .replace(
            "__LIMITERS__",
            _limiter_card(
                status.get("limiter_rows") or [],
                status.get("limiters") or {},
                controls if controls is not None else rev_limiters.controls_status(),
                payload["range"],
            ),
        )
        .replace("__BENCH__", _bench_card(tachometer_root))
        .replace("__MESSAGE__", _message(message, message_ok))
        .replace("__DATA__", data)
    )
