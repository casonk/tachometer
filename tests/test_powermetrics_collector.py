"""Tests for the root-run macOS powermetrics collector (no root or macOS needed)."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "macos" / "powermetrics_collector.py"
_SPEC = importlib.util.spec_from_file_location("powermetrics_collector", _PATH)
collector = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(collector)

INTEL = {
    "thermal_pressure": "Nominal",
    "smc": {"cpu_die": 61.5, "gpu_die": 55.0, "fan": 2150.0},
    "processor": {"cpu_power": 8450.0},
}
APPLE_SILICON = {
    "thermal_pressure": "Fair",
    "processor": {"cpu_power": 3120.0, "gpu_power": 410.0, "combined_power": 3600.0},
}


def _runner(accepts: set[str], sample: dict):
    """Fake powermetrics that rejects any sampler outside `accepts`."""
    calls = []

    def run(args, **kwargs):
        samplers = set(args[args.index("--samplers") + 1].split(","))
        calls.append(samplers)
        if not samplers <= accepts:
            return subprocess.CompletedProcess(args, 1, b"", b"unrecognized sampler")
        return subprocess.CompletedProcess(args, 0, plistlib.dumps(sample) + b"\0", b"")

    run.calls = calls
    return run


def test_intel_reading_keeps_die_temperatures_and_fan():
    record = collector.collect(_runner({"smc", "thermal", "cpu_power", "gpu_power"}, INTEL))

    assert record["cpu_die_c"] == 61.5
    assert record["gpu_die_c"] == 55.0
    assert record["fan_rpm"] == 2150
    assert record["thermal_pressure"] == "Nominal"
    assert record["cpu_power_w"] == 8.45


def test_apple_silicon_falls_back_without_the_smc_sampler():
    run = _runner({"thermal", "cpu_power", "gpu_power"}, APPLE_SILICON)

    record = collector.collect(run)

    assert run.calls[0] == {"smc", "thermal", "cpu_power", "gpu_power"}
    assert record["samplers"] == ["thermal", "cpu_power", "gpu_power"]
    assert record["cpu_die_c"] is None
    assert record["thermal_pressure"] == "Fair"
    assert record["gpu_power_w"] == 0.41


def test_no_usable_sampler_gives_none():
    assert collector.collect(_runner(set(), INTEL)) is None


def test_append_keeps_the_newest_lines_and_is_world_readable(tmp_path):
    out = tmp_path / "db" / "powermetrics.jsonl"
    for n in range(5):
        collector.append_line(str(out), {"n": n}, keep=3)

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines == [{"n": 2}, {"n": 3}, {"n": 4}]
    assert out.stat().st_mode & 0o777 == 0o644
