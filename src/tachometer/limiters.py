"""Rev limiters: the caps that bound how hard this machine may run.

A tachometer shows how fast the engine turns; the rev limiter is the ceiling
it cannot pass. On a workstation those ceilings are the GPU power cap, the
CPU's RAPL package power limits, its frequency ceiling and scaling policy, and
the system power profile. They change rarely but explain a lot: a temperature
plateau is often a limiter doing its job, and an unexpectedly hot machine is
often one with no limiter set. Each host sample records them, together with
whether the GPU is pressing against a limit at that moment.

Everything is read without root: sysfs for the CPU, ``nvidia-smi`` for the
GPU, and the tuned or power-profiles-daemon state for the profile. Missing
sources are left out rather than guessed.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_GPU_FIELDS = (
    "name",
    "power.limit",
    "power.default_limit",
    "power.min_limit",
    "power.max_limit",
    "power.draw",
    "persistence_mode",
    "clocks_event_reasons.active",
)
# nvidia-smi clocks_event_reasons bit flags (formerly "throttle reasons").
_GPU_REASONS = {
    0x2: "applications clocks",
    0x4: "power cap",
    0x8: "hardware slowdown",
    0x20: "thermal (software)",
    0x40: "thermal (hardware)",
    0x80: "power brake",
}


def _number(text: str) -> float | None:
    with contextlib.suppress(ValueError):
        return float(text)
    return None


def _run_nvidia_smi() -> str:
    tool = shutil.which("nvidia-smi")
    if not tool:
        return ""
    with contextlib.suppress(Exception):
        out = subprocess.run(
            [tool, f"--query-gpu={','.join(_GPU_FIELDS)}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout
    return ""


def gpu_limiters(query=_run_nvidia_smi) -> list[dict[str, Any]]:
    """Per-GPU power cap against its default and range, live draw, and active limits."""
    gpus = []
    for line in query().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_GPU_FIELDS):
            continue
        name, limit, default, low, high, draw, persistence, reasons = parts
        mask = int(reasons, 16) if reasons.lower().startswith("0x") else 0
        gpus.append(
            {
                "name": name,
                "power_limit_w": _number(limit),
                "default_limit_w": _number(default),
                "min_limit_w": _number(low),
                "max_limit_w": _number(high),
                "power_draw_w": _number(draw),
                "persistence": persistence.lower() == "enabled",
                # What is holding the clocks down right now, idle excluded.
                "limiting": [label for bit, label in _GPU_REASONS.items() if mask & bit],
            }
        )
    return gpus


def _read(path: Path) -> str | None:
    with contextlib.suppress(OSError):
        return path.read_text(encoding="utf-8").strip()
    return None


def _int(path: Path) -> int | None:
    value = _read(path)
    return int(value) if value is not None and value.lstrip("-").isdigit() else None


def rapl_limits(powercap: Path) -> list[dict[str, Any]]:
    """Intel RAPL package power limits (PL1 long-term, PL2 short-term), in watts."""
    zones = []
    for zone in sorted(powercap.glob("intel-rapl:*")):
        if zone.name.count(":") != 1:  # package zones only; sub-zones are core/uncore/dram
            continue
        entry: dict[str, Any] = {
            "zone": _read(zone / "name") or zone.name,
            "enabled": _read(zone / "enabled") == "1",
        }
        for index in range(4):
            name = _read(zone / f"constraint_{index}_name")
            limit = _int(zone / f"constraint_{index}_power_limit_uw")
            if not name or limit is None:
                continue
            entry[f"{name}_w"] = round(limit / 1e6, 1)
            window = _int(zone / f"constraint_{index}_time_window_us")
            if window is not None:
                entry[f"{name}_window_s"] = round(window / 1e6, 4)
        zones.append(entry)
    return zones


def cpu_frequency_limits(cpu_dir: Path) -> dict[str, Any]:
    """Frequency ceiling against the hardware maximum, governor, energy bias, turbo."""
    policy = cpu_dir / "cpu0" / "cpufreq"
    limits: dict[str, Any] = {}
    ceiling, hardware = _int(policy / "scaling_max_freq"), _int(policy / "cpuinfo_max_freq")
    if ceiling is not None:
        limits["max_mhz"] = ceiling // 1000
    if hardware is not None:
        limits["hardware_max_mhz"] = hardware // 1000
    for key, file in (
        ("governor", policy / "scaling_governor"),
        ("energy_preference", policy / "energy_performance_preference"),
    ):
        value = _read(file)
        if value:
            limits[key] = value
    no_turbo = _read(cpu_dir / "intel_pstate" / "no_turbo")
    if no_turbo is None:
        boost = _read(cpu_dir / "cpufreq" / "boost")  # acpi-cpufreq and amd-pstate
        if boost is not None:
            limits["turbo"] = boost == "1"
    else:
        limits["turbo"] = no_turbo == "0"
    max_perf = _int(cpu_dir / "intel_pstate" / "max_perf_pct")
    if max_perf is not None:
        limits["max_perf_pct"] = max_perf
    return limits


def _power_profiles_daemon() -> str | None:
    tool = shutil.which("powerprofilesctl")
    if not tool:
        return None
    with contextlib.suppress(Exception):
        out = subprocess.run([tool, "get"], capture_output=True, text=True, timeout=5, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


_WHOLE_DISK = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")
# Scheduler each drive type should run, as fedora-debugg's install_io_tuning.sh sets it.
RECOMMENDED_SCHEDULER = {"nvme": "none", "ssd": "mq-deadline", "hdd": "bfq"}


def disk_limits(sys_block: Path) -> list[dict[str, Any]]:
    """I/O scheduler and read-ahead per whole disk, with its drive type."""
    disks = []
    for base in sorted(sys_block.glob("*")):
        if not _WHOLE_DISK.match(base.name):
            continue
        queue = base / "queue"
        scheduler_line = _read(queue / "scheduler") or ""
        schedulers = scheduler_line.replace("[", "").replace("]", "").split()
        match = re.search(r"\[([^\]]+)\]", scheduler_line)
        scheduler = match.group(1) if match else (scheduler_line or None)
        kind = (
            "nvme"
            if base.name.startswith("nvme")
            else ("hdd" if _read(queue / "rotational") == "1" else "ssd")
        )
        disk = {
            "dev": base.name,
            "model": " ".join((_read(base / "device" / "model") or "").split()) or None,
            "kind": kind,
            "scheduler": scheduler,
            "schedulers": schedulers,
            "read_ahead_kb": _int(queue / "read_ahead_kb"),
        }
        volumes = _volumes(base)
        if volumes:
            disk["volumes"] = volumes
        disks.append(disk)
    return disks


def _volumes(base: Path) -> list[dict[str, Any]]:
    """Device-mapper volumes (LUKS, LVM) stacked on a disk or its partitions.

    Reads go through these, and each keeps its own read-ahead.
    """
    holders = list(base.glob("holders/*")) + list(base.glob("*/holders/*"))
    volumes = []
    for holder in sorted(holders, key=lambda p: p.name):
        name = _read(holder / "dm" / "name") or holder.name
        volumes.append(
            {
                "name": name,
                "dev": holder.name,
                "read_ahead_kb": _int(holder / "queue" / "read_ahead_kb"),
            }
        )
    return volumes


# PCI classes worth a link row: display, NVMe, Ethernet.
_LINK_CLASSES = {"0x0300": "GPU", "0x0302": "GPU", "0x0108": "NVMe", "0x0200": "Network"}
_PCIE_GEN = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}


def _gen(speed: str | None) -> int | None:
    return _PCIE_GEN.get((speed or "").split(" ")[0])


def pcie_links(pci_devices: Path) -> list[dict[str, Any]]:
    """Each GPU, NVMe drive and NIC: the link it has against what it and its slot support.

    The slot's (upstream port's) maximum is a ceiling set by the board; the
    current speed can also be lower for power saving while idle.
    """
    links = []
    for device in sorted(pci_devices.glob("*")):
        kind = _LINK_CLASSES.get((_read(device / "class") or "")[:6])
        width, device_width = _int(device / "current_link_width"), _int(device / "max_link_width")
        if not kind or not width or not device_width or device_width > 32:
            continue  # integrated devices report no real link
        port = device.resolve().parent
        name = next((p.name for p in device.glob("nvme/nvme*")), None) or next(
            (p.name for p in device.glob("net/*")), None
        )
        model = _read(device / "nvme" / name / "model") if name and kind == "NVMe" else None
        if not name:
            name = (
                Path(os.path.realpath(device / "driver")).name
                if (device / "driver").exists()
                else None
            )
        links.append(
            {
                "address": device.name,
                "kind": kind,
                "name": name,
                "model": " ".join(model.split()) if model else None,
                "width": width,
                "device_width": device_width,
                "slot_width": _int(port / "max_link_width"),
                "gen": _gen(_read(device / "current_link_speed")),
                "device_gen": _gen(_read(device / "max_link_speed")),
                "slot_gen": _gen(_read(port / "max_link_speed")),
            }
        )
    return links


def _run_ethtool_rings(iface: str) -> str:
    try:
        return subprocess.run(
            ["ethtool", "-g", iface], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def nic_limits(net: Path, rings=_run_ethtool_rings) -> list[dict[str, Any]]:
    """Physical network interfaces: speed, queues, ring sizes and hardware drop counters."""
    nics = []
    for iface in sorted(net.glob("*")):
        if not (iface / "device").exists():
            continue
        nic: dict[str, Any] = {
            "name": iface.name,
            "driver": Path(os.path.realpath(iface / "device" / "driver")).name or None,
            "speed_mbps": _int(iface / "speed"),
            "rx_queues": len(list(iface.glob("queues/rx-*"))),
            "rx_missed": _int(iface / "statistics" / "rx_missed_errors"),
            "rx_fifo": _int(iface / "statistics" / "rx_fifo_errors"),
            "carrier_changes": _int(iface / "carrier_changes"),
        }
        # "Pre-set maximums" come first in ethtool -g output, then "Current hardware settings".
        values = re.findall(r"^(RX|TX):\s+(\d+)", rings(iface.name), re.MULTILINE)
        if len(values) >= 4:
            nic["ring_max"] = {k: int(v) for k, v in values[:2]}
            nic["ring"] = {k: int(v) for k, v in values[2:4]}
        nics.append(nic)
    return nics


def power_profile(etc: Path = Path("/etc"), ppd=_power_profiles_daemon) -> dict[str, str]:
    tuned = _read(etc / "tuned" / "active_profile")
    if tuned:
        return {"source": "tuned", "name": tuned}
    name = ppd()
    return {"source": "power-profiles-daemon", "name": name} if name else {}


def read_limiters(
    sys_class: Path = Path("/sys/class"),
    *,
    cpu_dir: Path | None = None,
    sys_block: Path | None = None,
    etc: Path = Path("/etc"),
    gpu_query=_run_nvidia_smi,
    ppd=_power_profiles_daemon,
    pci_devices: Path | None = None,
    rings=_run_ethtool_rings,
) -> dict[str, Any]:
    """Every cap readable on this host; sections without a source are omitted."""
    cpu_dir = cpu_dir if cpu_dir is not None else sys_class.parent / "devices" / "system" / "cpu"
    limiters: dict[str, Any] = {}
    gpus = gpu_limiters(gpu_query)
    if gpus:
        limiters["gpu"] = gpus
    rapl = rapl_limits(sys_class / "powercap")
    frequency = cpu_frequency_limits(cpu_dir)
    if rapl or frequency:
        limiters["cpu"] = {**({"rapl": rapl} if rapl else {}), **frequency}
    disks = disk_limits(sys_block if sys_block is not None else sys_class.parent / "block")
    if disks:
        limiters["disks"] = disks
    links = pcie_links(
        pci_devices if pci_devices is not None else sys_class.parent / "bus" / "pci" / "devices"
    )
    if links:
        limiters["pcie"] = links
    nics = nic_limits(sys_class / "net", rings)
    if nics:
        limiters["network"] = nics
    profile = power_profile(etc, ppd)
    if profile:
        limiters["profile"] = profile
    return limiters


def describe(limiters: dict[str, Any]) -> list[dict[str, str]]:
    """Plain-language rows for the page: what each limiter is set to, and whether it bites."""
    rows: list[dict[str, str]] = []
    for gpu in limiters.get("gpu") or []:
        limit, default = gpu.get("power_limit_w"), gpu.get("default_limit_w")
        if limit is not None:
            capped = default is not None and limit < default - 0.5
            setting = f"{limit:.0f} W" + (
                f" (capped from {default:.0f} W)" if capped else " (stock)"
            )
            draw = gpu.get("power_draw_w")
            state = f"drawing {draw:.0f} W" if draw is not None else ""
            if gpu.get("limiting"):
                state += ("; " if state else "") + "limited by " + ", ".join(gpu["limiting"])
            rows.append(
                {
                    "limiter": f"GPU power — {gpu.get('name', 'GPU')}",
                    "setting": setting,
                    "state": state,
                    "light": "yellow" if gpu.get("limiting") else ("green" if capped else "none"),
                }
            )
    cpu = limiters.get("cpu") or {}
    for zone in cpu.get("rapl") or []:
        parts = []
        if "long_term_w" in zone:
            parts.append(f"{zone['long_term_w']:.0f} W sustained")
        if "short_term_w" in zone:
            parts.append(f"{zone['short_term_w']:.0f} W burst")
        rows.append(
            {
                "limiter": f"CPU power (RAPL {zone['zone']})",
                "setting": ", ".join(parts) or "–",
                "state": "enforced" if zone.get("enabled") else "disabled",
                "light": "green" if zone.get("enabled") else "none",
            }
        )
    if "max_mhz" in cpu:
        hardware = cpu.get("hardware_max_mhz")
        capped = hardware is not None and cpu["max_mhz"] < hardware
        bits = [f"{cpu['max_mhz'] / 1000:.1f} GHz ceiling"]
        if capped:
            bits.append(f"of {hardware / 1000:.1f} GHz")
        if "turbo" in cpu:
            bits.append("turbo on" if cpu["turbo"] else "turbo off")
        policy = " / ".join(v for v in (cpu.get("governor"), cpu.get("energy_preference")) if v)
        rows.append(
            {
                "limiter": "CPU frequency",
                "setting": ", ".join(bits),
                "state": policy,
                "light": "green" if capped or cpu.get("turbo") is False else "none",
            }
        )
    for disk in limiters.get("disks") or []:
        kind = {"nvme": "NVMe", "ssd": "SSD", "hdd": "HDD"}[disk["kind"]]
        recommended = RECOMMENDED_SCHEDULER[disk["kind"]]
        scheduler = disk.get("scheduler") or "?"
        read_ahead = disk.get("read_ahead_kb")
        matches = scheduler == recommended
        rows.append(
            {
                "limiter": f"Disk I/O — {disk['dev']}"
                + (f" {disk['model']}" if disk.get("model") else "")
                + f" ({kind})",
                "setting": f"{scheduler}"
                + (f", {read_ahead} KiB read-ahead" if read_ahead is not None else "")
                + "".join(
                    f"; volume {v['name']} {v['read_ahead_kb']} KiB"
                    for v in disk.get("volumes") or []
                    if v.get("read_ahead_kb") is not None
                ),
                "state": f"tuned for {kind}" if matches else f"{recommended} suits {kind} better",
                "light": "green" if matches else "yellow",
            }
        )
    for link in limiters.get("pcie") or []:
        label = " ".join(v for v in (link.get("name"), link.get("model")) if v) or link["address"]
        notes = []
        if link["width"] < link["device_width"]:
            slot = link.get("slot_width")
            notes.append(
                f"slot provides x{slot}" if slot and slot < link["device_width"]
                else "lanes shared or trained down"
            )  # fmt: skip
        slot_gen, device_gen = link.get("slot_gen"), link.get("device_gen")
        if slot_gen and device_gen and slot_gen < device_gen:
            notes.append(f"PCIe {device_gen}.0 device in a {slot_gen}.0 slot")
        elif link.get("gen") and device_gen and link["gen"] < device_gen:
            notes.append("slower while idle (power saving)")
        held_back = link["width"] < link["device_width"] or bool(
            slot_gen and device_gen and slot_gen < device_gen
        )
        rows.append(
            {
                "limiter": f"PCIe link — {link['kind']} {label}",
                "setting": f"x{link['width']} of x{link['device_width']}"
                + (f", PCIe {link['gen']}.0" if link.get("gen") else ""),
                "state": ("held back by its slot: " if held_back else "") + "; ".join(notes)
                or "full link",
                "light": "yellow" if held_back else "green",
            }
        )
    for nic in limiters.get("network") or []:
        bits = [f"{nic['speed_mbps']} Mb/s" if nic.get("speed_mbps") else "link down"]
        bits.append(f"{nic['rx_queues']} queue{'s' if nic['rx_queues'] != 1 else ''}")
        if nic.get("ring"):
            at_max = nic["ring"] == nic.get("ring_max")
            bits.append(
                f"ring {nic['ring']['RX']}/{nic['ring']['TX']}"
                + (" (hardware maximum)" if at_max else f" of {nic['ring_max']['RX']} max")
            )
        lost = (nic.get("rx_missed") or 0) + (nic.get("rx_fifo") or 0)
        rows.append(
            {
                "limiter": f"Network — {nic['name']}"
                + (f" ({nic['driver']})" if nic.get("driver") else ""),
                "setting": ", ".join(bits),
                "state": (
                    f"{lost:,} packets lost by the card" if lost else "no packets lost by the card"
                )
                + (
                    f"; {nic['carrier_changes']} link changes since boot"
                    if nic.get("carrier_changes") is not None
                    else ""
                ),
                "light": "yellow" if lost else "green",
            }
        )
    profile = limiters.get("profile")
    if profile:
        rows.append(
            {
                "limiter": "Power profile",
                "setting": profile["name"],
                "state": profile["source"],
                "light": "none",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Changing limiters from the /thermal page
# ---------------------------------------------------------------------------

# Root-owned helper installed by fedora-debugg's install_limiter_controls.sh,
# runnable through a sudoers rule without a password. It validates every value
# itself; the checks here only give the page a clear message sooner.
HELPER_ENV = "TACHOMETER_LIMITER_HELPER"
DEFAULT_HELPER = "/usr/local/sbin/fedora-debugg-limiter"
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DISK_DEV = re.compile(r"^(sd[a-z]{1,3}|nvme\d{1,3}n\d{1,3})$")
_VOLUME_DEV = re.compile(r"^dm-\d{1,4}$")
READ_AHEAD_CHOICES = (128, 256, 512, 1024, 2048, 4096, 8192, 16384)


def helper_path() -> str:
    return os.environ.get(HELPER_ENV, "").strip() or DEFAULT_HELPER


def tuned_profiles(run=subprocess.run) -> list[str]:
    """Profiles tuned offers (no root needed), in its own order."""
    if not shutil.which("tuned-adm"):
        return []
    with contextlib.suppress(Exception):
        out = run(["tuned-adm", "list"], capture_output=True, text=True, timeout=10, check=False)
        return [
            line.split()[1]
            for line in out.stdout.splitlines()
            if line.startswith("- ") and len(line.split()) > 1
        ]
    return []


def controls_status(run=subprocess.run, exists=os.path.exists) -> dict[str, Any]:
    """Whether the page may change limiters, and the choices it can offer."""
    helper = helper_path()
    status: dict[str, Any] = {"available": False, "helper": helper, "profiles": []}
    if not exists(helper):
        status["reason"] = "not installed"
        return status
    with contextlib.suppress(Exception):
        out = run(
            ["sudo", "-n", helper, "check"], capture_output=True, text=True, timeout=10, check=False
        )
        if out.returncode == 0:
            status["available"] = True
            status["profiles"] = tuned_profiles(run)
            return status
    status["reason"] = "no passwordless sudo rule for this user"
    return status


def apply_setting(kind: str, value: str, run=subprocess.run) -> tuple[bool, str]:
    """Run the helper for one change; returns (succeeded, message for the page)."""
    value = value.strip()
    if kind == "gpu_power":
        if not value.isdigit() or not 10 <= int(value) <= 2000:
            return False, "Enter the GPU power cap as whole watts."
        args = ["gpu-power", str(int(value))]
    elif kind == "profile":
        if not _PROFILE_NAME.match(value):
            return False, "Choose a power profile from the list."
        args = ["profile", value]
    elif kind in ("disk", "disk_reset"):
        return _apply_disk(kind, value, run)
    else:
        return False, "Unknown setting."
    return _run_helper(args, run)


def _run_helper(args: list[str], run=subprocess.run) -> tuple[bool, str]:
    try:
        out = run(
            ["sudo", "-n", helper_path(), *args],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run the limiter helper: {exc}"
    lines = [line for line in (out.stdout + "\n" + out.stderr).splitlines() if line.strip()]
    message = lines[-1].removeprefix("fedora-debugg-limiter: ") if lines else "Done."
    return out.returncode == 0, message


def _apply_disk(kind: str, value: str, run=subprocess.run) -> tuple[bool, str]:
    """``value`` is "dev", or "dev|scheduler|read_ahead_kb" with empty parts left unchanged."""
    dev, _, rest = value.partition("|")
    if not (_DISK_DEV.match(dev) or _VOLUME_DEV.match(dev)):
        return False, "Choose a disk or volume from the list."
    if kind == "disk_reset":
        return _run_helper(["disk-reset", dev], run)
    scheduler, _, read_ahead = rest.partition("|")
    steps = []
    if scheduler:
        if not _DISK_DEV.match(dev) or not _PROFILE_NAME.match(scheduler):
            return False, "Choose a scheduler from the list."
        steps.append(["disk-scheduler", dev, scheduler])
    if read_ahead:
        if not read_ahead.isdigit() or int(read_ahead) not in READ_AHEAD_CHOICES:
            return False, "Choose a read-ahead from the list."
        steps.append(["read-ahead", dev, read_ahead])
    if not steps:
        return False, "Nothing changed."
    messages = []
    for step in steps:
        ok, message = _run_helper(step, run)
        messages.append(message)
        if not ok:
            return False, message
    return True, "; ".join(messages)
