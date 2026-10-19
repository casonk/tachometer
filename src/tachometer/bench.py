"""Nightly benchmark tiers: short weekly, mid monthly, full quarterly.

A benchmark here exists to explain settings later (why a read-ahead, cap or
BIOS option is where it is) and to catch regressions: a drive that dropped to
a narrower link, a setting lost after a BIOS update, a fan that stopped. So
every run is recorded, including the ones that were skipped or cut short.

Guardrails, because the machine runs 24/7 and is managed remotely:
- ``auto`` runs only inside the quiet window (02:00-05:00 local) and only the
  most overdue tier, so at most one run a night;
- nothing runs while the library is being streamed; the AI upscaler (and any
  other unit in ``pause_units``) is frozen for the run and thawed after, so a
  render in progress continues instead of starting over;
- the machine must be idle after that (load per thread, GPU utilisation);
- every load stage stops as soon as the GPU or CPU passes its limit (GPU
  80 C, CPU 78 C, median of recent readings) and the run is marked
  aborted rather than retried;
- one run at a time (lock file), lowest CPU and I/O priority, reads only.

Settings live in config/tachometer/bench.local.toml (gitignored; see
bench.toml.example): which files to read per volume, the units to pause and
an optional Python with PyTorch for the GPU compute test.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import mmap
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import _read_cpu_temp

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

HISTORY_NAME = "bench-history.jsonl"
CONFIG_NAME = "bench.local.toml"
LOCK_NAME = "bench.lock"
# Days between runs of each tier; a run of a longer tier also counts for the shorter ones.
INTERVAL_DAYS = {"full": 90, "mid": 30, "short": 7}
TIER_ORDER = ("full", "mid", "short")
QUIET_HOURS = (2, 5)
# GPU limit above the upscaler's sustained 72 C so a soak can reach steady state,
# still below the RTX 3090's own ~83 C slowdown point.
MAX_GPU_C = float(os.environ.get("TACHOMETER_BENCH_MAX_GPU_C", "80"))
MAX_CPU_C = float(os.environ.get("TACHOMETER_BENCH_MAX_CPU_C", "78"))
IDLE_LOAD_PER_THREAD = 0.3
IDLE_GPU_UTIL = 15.0
CHUNK = 128 * 1024


@dataclass
class TierPlan:
    """What one tier measures and for how long."""

    read_runs: int
    read_mb: int
    random_pages: int
    memory_reps: int
    cpu_seconds: int
    gpu_seconds: int
    combined_seconds: int = 0


PLANS = {
    "short": TierPlan(read_runs=1, read_mb=512, random_pages=0, memory_reps=3,
                      cpu_seconds=0, gpu_seconds=0),
    "mid": TierPlan(read_runs=3, read_mb=2048, random_pages=2000, memory_reps=5,
                    cpu_seconds=600, gpu_seconds=600),
    "full": TierPlan(read_runs=5, read_mb=2048, random_pages=4000, memory_reps=9,
                     cpu_seconds=1800, gpu_seconds=1800, combined_seconds=1800),
}  # fmt: skip


@dataclass
class BenchConfig:
    volumes: dict[str, str] = field(default_factory=dict)
    pause_units: list[str] = field(default_factory=lambda: ["clockwork-upscale.service"])
    busy_units: list[str] = field(default_factory=lambda: ["clockwork-web.service"])
    torch_python: str = ""
    settle_seconds: int = 120
    # unit -> status file. Such a unit is stopped (not frozen) between two jobs:
    # a frozen GPU worker keeps its CUDA/NVENC session open and the card in P2,
    # drawing ~120-140 W at 0 % load, so GPU stages would start warm.
    stop_at_boundary: dict[str, str] = field(default_factory=dict)
    boundary_wait_minutes: int = 60


def load_config(path: Path) -> BenchConfig:
    if not path.exists():
        return BenchConfig()
    data = tomllib.loads(path.read_text())
    config = BenchConfig()
    config.volumes = {str(k): str(v) for k, v in (data.get("volumes") or {}).items()}
    for key in ("pause_units", "busy_units"):
        if key in data:
            setattr(config, key, [str(u) for u in data[key]])
    config.torch_python = str(data.get("torch_python", ""))
    config.settle_seconds = int(data.get("settle_seconds", config.settle_seconds))
    config.stop_at_boundary = {
        str(unit): os.path.expanduser(str(path))
        for unit, path in (data.get("stop_at_boundary") or {}).items()
    }
    config.boundary_wait_minutes = int(
        data.get("boundary_wait_minutes", config.boundary_wait_minutes)
    )
    return config


# ---------------------------------------------------------------------------
# History and scheduling
# ---------------------------------------------------------------------------


def history_path(root: Path) -> Path:
    return root / ".tachometer" / HISTORY_NAME


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        with contextlib.suppress(ValueError):
            records.append(json.loads(line))
    return records


def append_history(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def due_tier(history: list[dict[str, Any]], now: float) -> str | None:
    """The longest tier whose interval has passed since a run that covered it.

    Completed and aborted runs count (an aborted run still measured something
    and must not be retried the next night); skipped runs do not.
    """
    for tier in TIER_ORDER:
        covering = TIER_ORDER[: TIER_ORDER.index(tier) + 1]
        last = max(
            (
                r["started_at"]
                for r in history
                if r.get("tier") in covering and r.get("status") in ("completed", "aborted")
            ),
            default=None,
        )
        if last is None or now - last >= INTERVAL_DAYS[tier] * 86400 - 3600:
            return tier
    return None


def in_quiet_window(now: float, hours: tuple[int, int] = QUIET_HOURS) -> bool:
    hour = dt.datetime.fromtimestamp(now).hour
    return hours[0] <= hour < hours[1]


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


def gpu_reading() -> dict[str, float] | None:
    """Temperature, power, SM clock and utilisation of the first NVIDIA GPU."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,power.draw,clocks.sm,utilization.gpu,power.limit",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        temp, power, clock, util, limit = (float(x) for x in out[0].split(","))
    except ValueError:
        return None
    return {"temp_c": temp, "power_w": power, "sm_mhz": clock, "util": util, "limit_w": limit}


def nic_counters(sys_net: Path = Path("/sys/class/net")) -> dict[str, dict[str, Any]]:
    """Error and drop counters of each physical network interface."""
    found = {}
    for iface in sorted(sys_net.glob("*")):
        if not (iface / "device").exists():
            continue
        stats = {}
        for name in ("rx_dropped", "rx_errors", "rx_missed_errors", "rx_crc_errors",
                     "tx_errors", "tx_dropped"):  # fmt: skip
            with contextlib.suppress(OSError, ValueError):
                stats[name] = int((iface / "statistics" / name).read_text())
        for name in ("carrier_changes", "speed"):
            with contextlib.suppress(OSError, ValueError):
                stats[name] = int((iface / name).read_text())
        found[iface.name] = stats
    return found


def _evict(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def cold_read(path: str, limit_mb: int) -> float:
    """Sequential read() of a file evicted from the page cache, in MB/s."""
    _evict(path)
    fd = os.open(path, os.O_RDONLY)
    start, done = time.perf_counter(), 0
    try:
        while done < limit_mb << 20:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            done += len(chunk)
    finally:
        os.close(fd)
    return done / (time.perf_counter() - start) / 1e6


def random_reads(path: str, pages: int, seed: int) -> float:
    """Milliseconds to touch ``pages`` random 4 KiB pages of a cold file through mmap."""
    _evict(path)
    size = min(os.path.getsize(path), 4 << 30)
    with open(path, "rb") as handle:
        view = mmap.mmap(handle.fileno(), size, access=mmap.ACCESS_READ)
        chosen = random.Random(seed).sample(range(size // 4096), min(pages, size // 4096))
        start, total = time.perf_counter(), 0
        for page in chosen:
            total += view[page * 4096]
        elapsed = time.perf_counter() - start
        view.close()
    return elapsed * 1000


def memory_copy_gbps(size_mb: int = 1024, reps: int = 3) -> float:
    """Single-thread memcpy throughput (bytes copied per second), median of ``reps``."""
    src = bytearray(os.urandom(1 << 20)) * size_mb
    dst = bytearray(len(src))
    results = []
    for _ in range(reps):
        start = time.perf_counter()
        dst[:] = src
        results.append(len(src) / (time.perf_counter() - start) / 1e9)
    return statistics.median(results)


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 1)


# ---------------------------------------------------------------------------
# Load stages with a temperature guard
# ---------------------------------------------------------------------------


class Guard:
    """Samples temperatures while a load runs; trips at the limits."""

    def __init__(
        self,
        cpu_reader: Callable[[], float | None] = _read_cpu_temp,
        gpu_reader: Callable[[], dict[str, float] | None] = gpu_reading,
        max_cpu: float = MAX_CPU_C,
        max_gpu: float = MAX_GPU_C,
    ) -> None:
        self.cpu_reader, self.gpu_reader = cpu_reader, gpu_reader
        self.max_cpu, self.max_gpu = max_cpu, max_gpu
        self.trace: list[dict[str, float | None]] = []
        self.tripped: str | None = None

    def sample(self, started: float) -> None:
        gpu = self.gpu_reader() or {}
        self.trace.append(
            {
                "t": round(time.time() - started),
                "cpu_c": self.cpu_reader(),
                "gpu_c": gpu.get("temp_c"),
                "gpu_w": gpu.get("power_w"),
                "gpu_mhz": gpu.get("sm_mhz"),
            }
        )
        recent = self.trace[-3:]
        for key, limit, name in (("cpu_c", self.max_cpu, "CPU"), ("gpu_c", self.max_gpu, "GPU")):
            values = [r[key] for r in recent if r[key] is not None]
            # Median of the last readings: a single spike when a process wakes is not heat.
            if len(values) >= 2 and statistics.median(values) >= limit:
                self.tripped = f"{name} {statistics.median(values):.0f} °C"

    def summary(self) -> dict[str, Any]:
        def peak(key: str) -> float | None:
            values = [r[key] for r in self.trace if r[key] is not None]
            return max(values) if values else None

        def settled(key: str) -> float | None:
            values = [r[key] for r in self.trace[len(self.trace) // 2 :] if r[key] is not None]
            return round(statistics.median(values)) if values else None

        return {
            "peak_cpu_c": peak("cpu_c"),
            "peak_gpu_c": peak("gpu_c"),
            "peak_gpu_w": peak("gpu_w"),
            "settled_gpu_mhz": settled("gpu_mhz"),
            "settled_gpu_w": settled("gpu_w"),
            "trace": self.trace,
        }


def _start(command: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def run_load(
    commands: list[tuple[list[str], bool]], seconds: int, guard: Guard, interval: float = 5.0
) -> dict[str, Any]:
    """Run (command, repeat) pairs side by side for ``seconds`` or until the guard trips.

    A repeating command is started again whenever it finishes, and the output
    of each finished pass is kept; whatever is still running at the end is
    stopped, so only output it already flushed counts.
    """
    started = time.time()
    processes = [_start(command) for command, _repeat in commands]
    outputs = ["" for _ in commands]
    try:
        while time.time() - started < seconds and not guard.tripped:
            guard.sample(started)
            for index, (command, repeat) in enumerate(commands):
                if repeat and processes[index].poll() is not None:
                    outputs[index] += processes[index].communicate()[0] or ""
                    processes[index] = _start(command)
            if all(p.poll() is not None for p in processes):
                break
            time.sleep(interval)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for index, process in enumerate(processes):
            try:
                outputs[index] += process.communicate(timeout=30)[0] or ""
            except subprocess.TimeoutExpired:
                process.kill()
                outputs[index] += process.communicate()[0] or ""
    return {
        "seconds": round(time.time() - started),
        "stopped_by": guard.tripped,
        "outputs": outputs,
        **guard.summary(),
    }


def cpu_load_command() -> tuple[list[str], bool]:
    # One 7-Zip benchmark pass on all threads (~15 s), repeated for the stage.
    return ["7z", "b", "1", f"-mmt{os.cpu_count() or 1}"], True


def parse_7z(output: str) -> float | None:
    """Median rating (MIPS, compress and decompress averaged) over 7-Zip's result lines.

    Result lines look like ``22:  53759  1322  3956  52297 |  445998  1397  2722  38030``.
    """
    ratings = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 10 and parts[0].rstrip(":").isdigit() and parts[5] == "|":
            with contextlib.suppress(ValueError):
                ratings.append((float(parts[4]) + float(parts[9])) / 2)
    return round(statistics.median(ratings)) if ratings else None


GPU_MATMUL = """
import sys, time, torch
seconds = float(sys.argv[1]); n = 8192
a = torch.randn(n, n, device="cuda", dtype=torch.float16); b = torch.randn_like(a)
torch.cuda.synchronize(); end = time.time() + seconds
while time.time() < end:
    start, count = time.time(), 0
    while time.time() - start < 10:
        for _ in range(10):
            a @ b
        torch.cuda.synchronize(); count += 10
    print(f"tflops {2 * n**3 * count / (time.time() - start) / 1e12:.1f}", flush=True)
"""


def gpu_load_command(torch_python: str, seconds: int) -> tuple[list[str], bool] | None:
    if torch_python and Path(torch_python).exists():
        return [torch_python, "-c", GPU_MATMUL, str(seconds)], False
    return None


def parse_tflops(output: str) -> float | None:
    """Median of the ten-second FP16 matmul rates the GPU test printed."""
    rates = []
    for line in output.splitlines():
        if line.startswith("tflops "):
            with contextlib.suppress(ValueError):
                rates.append(float(line.split()[1]))
    return round(statistics.median(rates), 1) if rates else None


# ---------------------------------------------------------------------------
# Readiness: streaming, pausing units, idle
# ---------------------------------------------------------------------------


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False
    )


def unit_is_active(unit: str) -> bool:
    return _systemctl("is-active", unit).stdout.strip() == "active"


def pause_unit(unit: str) -> str | None:
    """Freeze a running unit in place (its work resumes after); stop it if freezing fails.

    Returns how it was paused ("frozen" or "stopped"), or None when it was not running.
    """
    if not unit_is_active(unit):
        return None
    if _systemctl("freeze", unit).returncode == 0:
        return "frozen"
    _systemctl("stop", unit)
    return "stopped"


def _job_marker(status_file: Path) -> tuple[Any, Any] | None:
    """(state, current job) from a worker's JSON status file, or None if unreadable."""
    try:
        status = json.loads(status_file.read_text())
    except (OSError, ValueError):
        return None
    return status.get("state"), status.get("source") or status.get("file")


def wait_for_boundary(
    status_file: Path, timeout: float, poll: float = 30.0, sleep=time.sleep, clock=time.time
) -> bool:
    """Wait until the worker is between jobs: idle, or moved on to its next job.

    Returns False if that did not happen within ``timeout`` seconds.
    """
    first = _job_marker(status_file)
    deadline = clock() + timeout
    while True:
        marker = _job_marker(status_file)
        if marker is not None and (
            marker[0] not in ("rendering", "cooling") or (first and marker[1] != first[1])
        ):
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


def pause_for_bench(unit: str, config: BenchConfig) -> str | None:
    """Stop a unit at a job boundary when it has a status file, otherwise freeze it."""
    status_file = config.stop_at_boundary.get(unit)
    if status_file and unit_is_active(unit):
        if wait_for_boundary(Path(status_file), config.boundary_wait_minutes * 60):
            _systemctl("stop", unit)
            return "stopped"
    return pause_unit(unit)


def resume_unit(unit: str, how: str) -> None:
    _systemctl("thaw" if how == "frozen" else "start", unit)


def ffmpeg_in_units(units: list[str], proc: Path = Path("/proc")) -> list[str]:
    """Units among ``units`` with an ffmpeg running in them (e.g. a library stream)."""
    busy = set()
    for pid in proc.glob("[0-9]*"):
        try:
            if (pid / "comm").read_text().strip() != "ffmpeg":
                continue
            cgroup = (pid / "cgroup").read_text()
        except OSError:
            continue
        busy.update(unit for unit in units if f"/{unit}" in cgroup)
    return sorted(busy)


def idle_problem(gpu: dict[str, float] | None) -> str | None:
    per_thread = os.getloadavg()[0] / (os.cpu_count() or 1)
    if per_thread > IDLE_LOAD_PER_THREAD:
        return f"load {per_thread:.2f} per thread"
    if gpu and gpu["util"] > IDLE_GPU_UTIL:
        return f"GPU {gpu['util']:.0f}% busy"
    return None


def _lower_priority() -> None:
    with contextlib.suppress(OSError):
        os.nice(19)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False, timeout=10)


# ---------------------------------------------------------------------------
# Running a tier
# ---------------------------------------------------------------------------


def cool_down(
    guard: Guard, margin: float = 10.0, timeout: float = 900, interval: float = 15
) -> bool:
    """Wait until CPU and GPU are ``margin`` below their limits; False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cpu = guard.cpu_reader()
        gpu = (guard.gpu_reader() or {}).get("temp_c")
        if (cpu is None or cpu < guard.max_cpu - margin) and (
            gpu is None or gpu < guard.max_gpu - margin
        ):
            return True
        time.sleep(interval)
    return False


def measure(tier: str, config: BenchConfig, guard_factory=Guard) -> dict[str, Any]:
    """Run one tier's measurements; returns results and whether a limit stopped it."""
    plan = PLANS[tier]
    results: dict[str, Any] = {"nic": nic_counters()}
    gpu = gpu_reading()
    results["idle"] = {"cpu_c": _read_cpu_temp(), "gpu": gpu}

    disks = {}
    for label, path in config.volumes.items():
        if not Path(path).is_file():
            disks[label] = {"error": "file missing"}
            continue
        reads = [cold_read(path, plan.read_mb) for _ in range(plan.read_runs)]
        entry: dict[str, Any] = {"read_MBps": _median(reads), "runs": [round(r) for r in reads]}
        if plan.random_pages:
            entry["random_ms"] = _median(
                [random_reads(path, plan.random_pages, seed) for seed in range(plan.read_runs)]
            )
        disks[label] = entry
    results["disks"] = disks
    results["memory_copy_GBps"] = round(memory_copy_gbps(reps=plan.memory_reps), 2)

    # A stage stopped by heat does not end the run: the next stage starts once
    # the machine has cooled 10 C below the limits (or the run ends if it does not).
    stopped_by = None
    stages = [("cpu", plan.cpu_seconds), ("gpu", plan.gpu_seconds),
              ("combined", plan.combined_seconds)]  # fmt: skip
    for stage, seconds in stages:
        if not seconds:
            continue
        if stopped_by and not cool_down(guard_factory()):
            results[stage] = {"skipped": "did not cool down after the previous stage"}
            continue
        gpu_cmd = gpu_load_command(config.torch_python, seconds)
        commands = {
            "cpu": [cpu_load_command()],
            "gpu": [gpu_cmd] if gpu_cmd else [],
            "combined": [cpu_load_command()] + ([gpu_cmd] if gpu_cmd else []),
        }[stage]
        if not commands:
            results[stage] = {"skipped": "no torch_python configured"}
            continue
        load = run_load(commands, seconds, guard_factory())
        outputs = load.pop("outputs")
        load["cpu_rating_mips"] = parse_7z(outputs[0]) if stage != "gpu" else None
        load["gpu_tflops"] = parse_tflops(outputs[-1]) if stage != "cpu" else None
        results[stage] = load
        stopped_by = stopped_by or load["stopped_by"]
    return {"results": results, "stopped_by": stopped_by}


def run(
    root: Path,
    tier: str | None,
    *,
    auto: bool = False,
    now: float | None = None,
    config: BenchConfig | None = None,
) -> dict[str, Any]:
    """Run a tier (or, with ``auto``, the overdue one inside the quiet window)."""
    now = time.time() if now is None else now
    config = config or load_config(root / "config" / "tachometer" / CONFIG_NAME)
    history = history_path(root)
    record: dict[str, Any] = {"started_at": now, "tier": tier, "host": platform.node()}

    def finish(status: str, **extra: Any) -> dict[str, Any]:
        record.update(status=status, duration_s=round(time.time() - now), **extra)
        append_history(history, record)
        return record

    if auto:
        tier = due_tier(load_history(history), now)
        record["tier"] = tier
        if tier is None:
            return {"status": "not-due"}  # nothing recorded: the normal nightly case
        if not in_quiet_window(now):
            return finish("skipped", reason="outside the quiet window")
    if tier not in PLANS:
        raise ValueError(f"unknown tier {tier!r}")

    lock_file = history.parent / LOCK_NAME
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return finish("skipped", reason="another benchmark is running")
        streaming = ffmpeg_in_units(config.busy_units)
        if streaming:
            return finish("skipped", reason=f"in use: {', '.join(streaming)}")
        _lower_priority()
        paused = {}
        for unit in config.pause_units:
            how = pause_for_bench(unit, config)
            if how:
                paused[unit] = how
        try:
            if paused:
                time.sleep(config.settle_seconds)
            problem = idle_problem(gpu_reading())
            if problem:
                return finish("skipped", reason=f"not idle: {problem}", paused=paused)
            record["kernel"] = platform.release()
            measured = measure(tier, config)
            status = "aborted" if measured["stopped_by"] else "completed"
            return finish(status, paused=paused, **measured)
        finally:
            for unit, how in paused.items():
                resume_unit(unit, how)


def headline(record: dict[str, Any]) -> dict[str, Any]:
    """The few numbers worth showing per run."""
    results = record.get("results") or {}
    loads = {k: results.get(k) or {} for k in ("cpu", "gpu", "combined")}
    return {
        "disks": {k: v.get("read_MBps") for k, v in (results.get("disks") or {}).items()},
        "memory_copy_GBps": results.get("memory_copy_GBps"),
        "cpu_rating_mips": loads["cpu"].get("cpu_rating_mips"),
        "gpu_tflops": loads["gpu"].get("gpu_tflops"),
        "peak_cpu_c": max(
            (v for v in (loads[k].get("peak_cpu_c") for k in loads) if v is not None),
            default=None,
        ),
        "peak_gpu_c": max(
            (v for v in (loads[k].get("peak_gpu_c") for k in loads) if v is not None),
            default=None,
        ),
    }


def main(root: Path, tier: str | None, auto: bool) -> int:
    record = run(root, tier, auto=auto)
    if record.get("status") == "not-due":
        print("no benchmark due")
        return 0
    summary = {k: record.get(k) for k in ("tier", "status", "reason", "stopped_by", "duration_s")}
    if record.get("results"):
        summary["headline"] = headline(record)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(Path.cwd(), sys.argv[1] if len(sys.argv) > 1 else None, auto=False))
