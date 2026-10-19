"""Tests for the nightly benchmark tiers: scheduling, guards, parsing and history."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from tachometer import bench, thermal

DAY = 86400.0


def _night(day: int = 1, hour: int = 3) -> float:
    return dt.datetime(2026, 10, day, hour, 0).timestamp()


def test_due_tier_picks_the_most_overdue_and_longer_runs_cover_shorter():
    now = _night(20)
    assert bench.due_tier([], now) == "full"

    full = {"tier": "full", "status": "completed", "started_at": now - 2 * DAY}
    assert bench.due_tier([full], now) is None  # a full run also counts as mid and short

    old_short = {"tier": "short", "status": "completed", "started_at": now - 8 * DAY}
    old_full = {"tier": "full", "status": "aborted", "started_at": now - 40 * DAY}
    assert bench.due_tier([old_full, old_short], now) == "mid"

    recent_mid = {"tier": "mid", "status": "completed", "started_at": now - 8 * DAY}
    assert bench.due_tier([old_full, recent_mid], now) == "short"

    skipped = {"tier": "full", "status": "skipped", "started_at": now - DAY}
    assert bench.due_tier([skipped], now) == "full"  # skipped nights do not count


def test_auto_runs_only_in_the_quiet_window(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bench, "measure", lambda tier, config: calls.append(tier))

    record = bench.run(tmp_path, None, auto=True, now=_night(hour=14), config=bench.BenchConfig())

    assert record["status"] == "skipped" and record["reason"] == "outside the quiet window"
    assert calls == []
    assert bench.load_history(bench.history_path(tmp_path))[0]["tier"] == "full"


def test_run_pauses_units_skips_when_busy_and_always_resumes(tmp_path, monkeypatch):
    actions = []
    monkeypatch.setattr(bench, "_lower_priority", lambda: None)
    monkeypatch.setattr(bench, "unit_is_active", lambda unit: True)
    monkeypatch.setattr(
        bench,
        "_systemctl",
        lambda *args: actions.append(args) or bench.subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(bench, "gpu_reading", lambda: {"util": 3.0})
    monkeypatch.setattr(bench, "idle_problem", lambda gpu: None)
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    monkeypatch.setattr(bench, "ffmpeg_in_units", lambda units: [])
    monkeypatch.setattr(
        bench, "measure", lambda tier, config: {"results": {}, "stopped_by": "GPU 73 °C"}
    )
    config = bench.BenchConfig(pause_units=["clockwork-upscale.service"])

    record = bench.run(tmp_path, "mid", now=_night(), config=config)

    assert record["status"] == "aborted" and record["stopped_by"] == "GPU 73 °C"
    assert actions == [
        ("freeze", "clockwork-upscale.service"),
        ("thaw", "clockwork-upscale.service"),
    ]
    assert record["paused"] == {"clockwork-upscale.service": "frozen"}

    monkeypatch.setattr(bench, "ffmpeg_in_units", lambda units: ["clockwork-web.service"])
    busy = bench.run(tmp_path, "short", now=_night(), config=config)
    assert busy["status"] == "skipped" and "clockwork-web" in busy["reason"]
    assert len(actions) == 2  # nothing paused when skipping for a stream


def test_guard_trips_on_sustained_heat_not_a_single_spike():
    readings = iter([60.0, 95.0, 61.0, 62.0, 79.0, 80.0])
    guard = bench.Guard(cpu_reader=lambda: next(readings), gpu_reader=lambda: None)
    for _ in range(5):
        guard.sample(0)
    assert guard.tripped is None  # the 95 °C spike alone never trips it
    guard.sample(0)
    assert guard.tripped == "CPU 79 °C"


def test_run_load_repeats_passes_and_keeps_their_output():
    guard = bench.Guard(cpu_reader=lambda: 50.0, gpu_reader=lambda: None)
    command = [sys.executable, "-c", "print('pass')"]

    load = bench.run_load([(command, True)], seconds=1, guard=guard, interval=0.2)

    assert load["outputs"][0].count("pass") >= 2
    assert load["stopped_by"] is None and load["peak_cpu_c"] == 50.0


def test_parsers_read_7zip_and_gpu_rates():
    sevenzip = """
22:      53759  1322   3956  52297  |     445998  1397   2722  38030
23:      48926  1321   3774  49850  |     466847  1483   2723  40386
Avr:     30682   720   4919  32197  |     272007   774   3353  23693
"""
    assert bench.parse_7z(sevenzip) == round((45163.5 + 45118) / 2)
    assert bench.parse_tflops("tflops 60.1\ntflops 61.0\ntflops 59.0\n") == 60.1
    assert bench.parse_7z("") is None and bench.parse_tflops("") is None


def test_cold_read_and_memory_copy_measure_something(tmp_path):
    data = tmp_path / "file.bin"
    data.write_bytes(b"x" * (4 << 20))
    assert bench.cold_read(str(data), 4) > 0
    assert bench.random_reads(str(data), 50, seed=1) >= 0
    assert bench.memory_copy_gbps(size_mb=8, reps=1) > 0


def test_nic_counters_read_physical_interfaces_only(tmp_path):
    eth = tmp_path / "enp5s0"
    (eth / "statistics").mkdir(parents=True)
    (eth / "device").mkdir()
    (eth / "statistics" / "rx_dropped").write_text("12\n")
    (eth / "carrier_changes").write_text("2\n")
    (tmp_path / "lo" / "statistics").mkdir(parents=True)

    assert bench.nic_counters(tmp_path) == {"enp5s0": {"rx_dropped": 12, "carrier_changes": 2}}


def test_config_loads_volumes_and_units(tmp_path):
    path = tmp_path / "bench.local.toml"
    path.write_text('pause_units = []\ntorch_python = "/py"\n\n[volumes]\nhome = "/x/model.bin"\n')
    config = bench.load_config(path)
    assert config.volumes == {"home": "/x/model.bin"}
    assert config.pause_units == [] and config.torch_python == "/py"
    assert bench.load_config(tmp_path / "missing.toml").busy_units == ["clockwork-web.service"]


def test_thermal_page_lists_benchmark_runs(tmp_path):
    path = bench.history_path(tmp_path)
    bench.append_history(
        path,
        {
            "started_at": _night(),
            "tier": "mid",
            "status": "completed",
            "results": {
                "disks": {"data": {"read_MBps": 1404.0}},
                "memory_copy_GBps": 9.8,
                "cpu": {"cpu_rating_mips": 45000, "peak_cpu_c": 71.0},
                "gpu": {"gpu_tflops": 60.2, "peak_gpu_c": 69.0},
            },
        },
    )
    bench.append_history(
        path,
        {"started_at": _night(2), "tier": "short", "status": "skipped", "reason": "not idle"},
    )

    page = thermal.render_page(tmp_path, controls={})

    assert "<h2>Benchmarks</h2>" in page
    assert "data 1,404 MB/s" in page and "45,000 MIPS" in page and "60.2 TFLOPS" in page
    assert "not idle" in page
    assert page.index("<td>short</td>") < page.index("<td>mid</td>")  # newest first


def test_thermal_page_explains_an_empty_history(tmp_path):
    assert "No runs yet" in thermal.render_page(Path(tmp_path), controls={})


def test_a_heat_stop_waits_for_cool_down_then_runs_the_next_stage(monkeypatch):
    stages = []

    def fake_load(commands, seconds, guard):
        stages.append(len(commands))
        return {
            "outputs": [""] * len(commands),
            "stopped_by": "CPU 78 °C" if not stages[1:] else None,
        }

    monkeypatch.setattr(bench, "run_load", fake_load)
    monkeypatch.setattr(bench, "cool_down", lambda guard: True)
    monkeypatch.setattr(bench, "memory_copy_gbps", lambda reps: 10.0)
    monkeypatch.setattr(bench, "gpu_reading", lambda: None)
    monkeypatch.setattr(bench.Path, "exists", lambda self: True)
    config = bench.BenchConfig(torch_python="/py")

    measured = bench.measure("full", config)

    assert stages == [1, 1, 2]  # cpu, gpu, combined all ran
    assert measured["stopped_by"] == "CPU 78 °C"


def test_cool_down_waits_until_below_the_margin(monkeypatch):
    temps = iter([77.0, 70.0, 65.0])
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    guard = bench.Guard(cpu_reader=lambda: next(temps), gpu_reader=lambda: None)
    assert bench.cool_down(guard) is True


def test_pause_falls_back_to_stop_when_freezing_fails(monkeypatch):
    actions = []

    def fake(*args):
        actions.append(args)
        return bench.subprocess.CompletedProcess(args, 1 if args[0] == "freeze" else 0)

    monkeypatch.setattr(bench, "_systemctl", fake)
    monkeypatch.setattr(bench, "unit_is_active", lambda unit: True)
    how = bench.pause_unit("x.service")
    bench.resume_unit("x.service", how)
    assert how == "stopped"
    assert actions == [("freeze", "x.service"), ("stop", "x.service"), ("start", "x.service")]
    monkeypatch.setattr(bench, "unit_is_active", lambda unit: False)
    assert bench.pause_unit("x.service") is None


def test_example_config_keeps_plain_settings_out_of_its_tables():
    example = (
        Path(__file__).resolve().parent.parent / "config" / "tachometer" / "bench.toml.example"
    )
    config = bench.load_config(example)
    assert config.volumes == {}
    assert config.pause_units == ["clockwork-upscale.service"]
    assert config.settle_seconds == 120 and config.boundary_wait_minutes == 60


def test_wait_for_boundary_sees_the_next_job_or_idle(tmp_path):
    status = tmp_path / "status.json"
    states = iter(
        [
            {"state": "rendering", "source": "/a.mkv"},
            {"state": "rendering", "source": "/a.mkv"},
            {"state": "rendering", "source": "/b.mkv"},
        ]
    )

    def advance(_seconds):
        status.write_text(json.dumps(next(states)))

    advance(0)
    assert bench.wait_for_boundary(status, 3600, sleep=advance, clock=lambda: 0.0) is True

    status.write_text(json.dumps({"state": "done"}))
    assert bench.wait_for_boundary(status, 0, sleep=lambda s: None) is True

    status.write_text(json.dumps({"state": "rendering", "source": "/a.mkv"}))
    clock = iter([0.0, 0.0, 100.0])
    assert (
        bench.wait_for_boundary(status, 50, sleep=lambda s: None, clock=lambda: next(clock))
        is False
    )


def test_pause_for_bench_stops_at_a_boundary_else_freezes(monkeypatch, tmp_path):
    actions = []
    monkeypatch.setattr(bench, "unit_is_active", lambda unit: True)
    monkeypatch.setattr(
        bench,
        "_systemctl",
        lambda *args: actions.append(args) or bench.subprocess.CompletedProcess(args, 0),
    )
    config = bench.BenchConfig(stop_at_boundary={"up.service": str(tmp_path / "s.json")})

    monkeypatch.setattr(bench, "wait_for_boundary", lambda path, timeout: True)
    assert bench.pause_for_bench("up.service", config) == "stopped"
    assert actions[-1] == ("stop", "up.service")

    monkeypatch.setattr(bench, "wait_for_boundary", lambda path, timeout: False)
    assert bench.pause_for_bench("up.service", config) == "frozen"
    assert bench.pause_for_bench("other.service", config) == "frozen"  # no status file


def test_thermal_card_shows_time_to_the_limit(tmp_path):
    bench.append_history(
        bench.history_path(tmp_path),
        {
            "started_at": _night(),
            "tier": "full",
            "status": "aborted",
            "stopped_by": "GPU 80 °C",
            "results": {
                "cpu": {"seconds": 1802},
                "gpu": {"seconds": 432, "stopped_by": "GPU 80 °C"},
            },
        },
    )
    assert "stopped at GPU 80 °C after 7 min (gpu stage)" in thermal.render_page(
        tmp_path, controls={}
    )


def test_stop_units_are_stopped_at_once(monkeypatch):
    actions = []
    monkeypatch.setattr(bench, "unit_is_active", lambda unit: True)
    monkeypatch.setattr(
        bench,
        "_systemctl",
        lambda *args: actions.append(args) or bench.subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(bench, "wait_for_boundary", lambda *a: pytest.fail("should not wait"))
    config = bench.BenchConfig(stop_units=["up.service"], stop_at_boundary={"up.service": "/s"})
    assert bench.pause_for_bench("up.service", config) == "stopped"
    assert actions == [("stop", "up.service")]
