"""Tests for the rev-limiter readings: GPU power cap, CPU RAPL and frequency limits, profile."""

from __future__ import annotations

import json
from pathlib import Path

from tachometer import limiters, thermal

GPU_CSV = (
    "NVIDIA GeForce RTX 3090, 250.00, 420.00, 100.00, 450.00, 189.68, Enabled, 0x0000000000000004\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _fake_host(root: Path) -> tuple[Path, Path, Path]:
    sys_class = root / "sys" / "class"
    zone = sys_class / "powercap" / "intel-rapl:0"
    _write(zone / "name", "package-0")
    _write(zone / "enabled", "1")
    _write(zone / "constraint_0_name", "long_term")
    _write(zone / "constraint_0_power_limit_uw", "95000000")
    _write(zone / "constraint_0_time_window_us", "27983872")
    _write(zone / "constraint_1_name", "short_term")
    _write(zone / "constraint_1_power_limit_uw", "210000000")
    _write(sys_class / "powercap" / "intel-rapl:0:0" / "name", "core")
    cpu = root / "sys" / "devices" / "system" / "cpu"
    _write(cpu / "cpu0" / "cpufreq" / "scaling_max_freq", "4200000")
    _write(cpu / "cpu0" / "cpufreq" / "cpuinfo_max_freq", "5000000")
    _write(cpu / "cpu0" / "cpufreq" / "scaling_governor", "powersave")
    _write(cpu / "cpu0" / "cpufreq" / "energy_performance_preference", "balance_power")
    _write(cpu / "intel_pstate" / "no_turbo", "0")
    _write(cpu / "intel_pstate" / "max_perf_pct", "84")
    etc = root / "etc"
    _write(etc / "tuned" / "active_profile", "balanced")
    return sys_class, cpu, etc


def test_gpu_cap_draw_and_active_limit_are_parsed():
    (gpu,) = limiters.gpu_limiters(lambda: GPU_CSV)

    assert gpu["power_limit_w"] == 250.0 and gpu["default_limit_w"] == 420.0
    assert (gpu["min_limit_w"], gpu["max_limit_w"]) == (100.0, 450.0)
    assert gpu["power_draw_w"] == 189.68
    assert gpu["persistence"] is True
    assert gpu["limiting"] == ["power cap"]


def test_gpu_reasons_decode_every_flag_and_bad_lines_are_skipped():
    csv = "GPU, 300, 300, 100, 350, [N/A], Disabled, 0x00000000000000E5\nnot,enough\n"
    (gpu,) = limiters.gpu_limiters(lambda: csv)
    assert gpu["power_draw_w"] is None and gpu["persistence"] is False
    assert gpu["limiting"] == [
        "power cap",
        "thermal (software)",
        "thermal (hardware)",
        "power brake",
    ]
    assert limiters.gpu_limiters(lambda: "") == []


def test_cpu_limits_and_profile_come_from_sysfs_and_tuned(tmp_path):
    sys_class, cpu, etc = _fake_host(tmp_path)

    caps = limiters.read_limiters(sys_class, cpu_dir=cpu, etc=etc, gpu_query=lambda: GPU_CSV)

    (zone,) = caps["cpu"]["rapl"]  # the core sub-zone is not a package limit
    assert zone == {
        "zone": "package-0",
        "enabled": True,
        "long_term_w": 95.0,
        "long_term_window_s": 27.9839,
        "short_term_w": 210.0,
    }
    assert caps["cpu"]["max_mhz"] == 4200 and caps["cpu"]["hardware_max_mhz"] == 5000
    assert caps["cpu"]["governor"] == "powersave"
    assert caps["cpu"]["energy_preference"] == "balance_power"
    assert caps["cpu"]["turbo"] is True and caps["cpu"]["max_perf_pct"] == 84
    assert caps["profile"] == {"source": "tuned", "name": "balanced"}
    json.dumps(caps)  # stored in the samples file as is


def test_missing_sources_are_left_out(tmp_path):
    caps = limiters.read_limiters(
        tmp_path / "sys" / "class", etc=tmp_path / "etc", gpu_query=lambda: "", ppd=lambda: None
    )
    assert caps == {}


def test_power_profiles_daemon_is_the_fallback_profile(tmp_path):
    assert limiters.power_profile(tmp_path, ppd=lambda: "power-saver") == {
        "source": "power-profiles-daemon",
        "name": "power-saver",
    }


def test_describe_reads_like_a_rev_limiter_panel(tmp_path):
    sys_class, cpu, etc = _fake_host(tmp_path)
    caps = limiters.read_limiters(sys_class, cpu_dir=cpu, etc=etc, gpu_query=lambda: GPU_CSV)

    rows = {row["limiter"]: row for row in limiters.describe(caps)}

    gpu = rows["GPU power — NVIDIA GeForce RTX 3090"]
    assert gpu["setting"] == "250 W (capped from 420 W)"
    assert gpu["state"] == "drawing 190 W; limited by power cap" and gpu["light"] == "yellow"
    assert rows["CPU power (RAPL package-0)"]["setting"] == "95 W sustained, 210 W burst"
    assert rows["CPU frequency"]["setting"] == "4.2 GHz ceiling, of 5.0 GHz, turbo on"
    assert rows["CPU frequency"]["light"] == "green"
    assert rows["Power profile"]["setting"] == "balanced"


def test_stock_gpu_without_a_cap_is_not_marked_as_limited():
    csv = "GPU, 420.00, 420.00, 100.00, 450.00, 30.00, Enabled, 0x0000000000000001\n"
    (row,) = limiters.describe({"gpu": limiters.gpu_limiters(lambda: csv)})
    assert row["setting"] == "420 W (stock)" and row["light"] == "none"
    assert row["state"] == "drawing 30 W"


def test_samples_record_limiters_and_chart_gpu_power(tmp_path):
    sys_class, cpu, etc = _fake_host(tmp_path)

    def reader():
        return limiters.read_limiters(sys_class, cpu_dir=cpu, etc=etc, gpu_query=lambda: GPU_CSV)

    root = tmp_path / "tacho"
    path = thermal.samples_path(root)
    for ts in (1000, 1300):
        sample = thermal.collect_sample(
            sys_class, now=ts, gpu_reader=lambda: 70.0, limits_reader=reader, platform="linux"
        )
        thermal.append_sample(path, sample)

    assert sample["gpu_w"] == 189.7 and sample["gpu_cap_w"] == 250.0
    assert sample["limiters"]["cpu"]["rapl"][0]["long_term_w"] == 95.0

    payload = thermal.build_payload(root, "24h", now=1400)
    assert payload["samples"][-1]["gpu_w"] == 189.7
    assert payload["status"]["limiters"]["profile"]["name"] == "balanced"
    page = thermal.render_page(root, "24h", now=1400)
    assert "Rev limiters" in page and "250 W (capped from 420 W)" in page
    assert "GPU power" in page


def test_hourly_buckets_keep_peak_gpu_power(tmp_path):
    points = thermal.hourly_series(
        [{"ts": 3600, "gpu_w": 120.0, "gpu_cap_w": 250.0}, {"ts": 3900, "gpu_w": 240.0}],
        {},
        since=0,
    )
    assert points[0]["gpu_w"] == 240.0 and points[0]["gpu_cap_w"] == 250.0


def test_macos_samples_skip_the_linux_limiters(tmp_path):
    called = []
    sample = thermal.collect_sample(
        tmp_path,
        now=5,
        gpu_reader=lambda: None,
        platform="darwin",
        macos_run=lambda _args: None,
        limits_reader=lambda: called.append(1) or {},
    )
    assert called == [] and "limiters" not in sample
