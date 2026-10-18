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


# --- fans ---------------------------------------------------------------------


def _fans_sysfs(root: Path) -> Path:
    _write(root / "hwmon" / "hwmon7" / "name", "nct6798")
    _write(root / "hwmon" / "hwmon7" / "fan1_input", "1230")
    _write(root / "hwmon" / "hwmon7" / "fan2_input", "1859")
    _write(root / "hwmon" / "hwmon7" / "fan2_label", "CPU_FAN")
    _write(root / "hwmon" / "hwmon7" / "fan10_input", "900")
    _write(root / "hwmon" / "hwmon7" / "fan4_input", "0")  # unconnected header
    return root


def test_fans_are_read_per_header_and_idle_headers_skipped(tmp_path):
    sysfs = _fans_sysfs(tmp_path)
    assert thermal._read_fans(sysfs) == {"fan1": 1230, "fan2": 1859, "fan10": 900}
    _write(sysfs / "hwmon" / "hwmon8" / "name", "asus")
    _write(sysfs / "hwmon" / "hwmon8" / "fan1_input", "700")
    assert set(thermal._read_fans(sysfs)) == {
        "nct6798/fan1", "nct6798/fan2", "nct6798/fan10", "asus/fan1",
    }  # fmt: skip


def test_fan_labels_prefer_configuration_then_hwmon_then_number(tmp_path, monkeypatch):
    sysfs = _fans_sysfs(tmp_path)
    monkeypatch.setenv(thermal.FAN_LABELS_ENV, "fan1=Rear exhaust, junk ,fan10 = Front")
    assert thermal.fan_labels(["fan1", "fan2", "fan10"], sysfs) == {
        "fan1": "Rear exhaust",
        "fan2": "CPU_FAN",
        "fan10": "Front",
    }
    monkeypatch.delenv(thermal.FAN_LABELS_ENV)
    assert thermal.fan_labels(["fan1"], sysfs) == {"fan1": "Fan 1"}


def test_fans_flow_into_samples_payload_hourly_history_and_page(tmp_path):
    sysfs = _fans_sysfs(tmp_path / "sys")
    root = tmp_path / "tacho"
    path = thermal.samples_path(root)
    for ts, gpu_fan in ((1000, 40.0), (1300, 65.0)):
        sample = thermal.collect_sample(
            sysfs,
            now=ts,
            gpu_reader=lambda: 60.0,
            limits_reader=lambda: {},
            gpu_fan_reader=lambda value=gpu_fan: value,
            platform="linux",
        )
        thermal.append_sample(path, sample)
    assert sample["fans"]["fan2"] == 1859 and sample["gpu_fan"] == 65.0

    payload = thermal.build_payload(root, "24h", now=1400)
    assert payload["samples"][-1]["fan:fan2"] == 1859
    assert payload["samples"][-1]["gpu_fan"] == 65.0
    assert set(payload["fan_labels"]) == {"fan1", "fan2", "fan10"}

    points = thermal.hourly_series(
        [
            {"ts": 3600, "fans": {"fan1": 1200}, "gpu_fan": 30.0},
            {"ts": 3900, "fans": {"fan1": 1500}, "gpu_fan": 55.0},
        ],
        {},
        since=0,
    )
    assert points[0]["fan:fan1"] == 1500 and points[0]["gpu_fan"] == 55.0

    page = thermal.render_page(root, "24h", now=1400, controls={"available": False})
    assert "<h2>Fans</h2>" in page and "GPU fan" in page


# --- controls -----------------------------------------------------------------


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, cmd, **_kwargs):
        import subprocess

        self.calls.append(cmd)
        if cmd[:2] == ["tuned-adm", "list"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "Available profiles:\n- balanced   - x\n- powersave  - y\nCurrent active profile: balanced\n",
                "",
            )
        code, out, err = self.result
        return subprocess.CompletedProcess(cmd, code, out, err)


def test_controls_need_the_helper_and_a_sudo_rule(monkeypatch):
    monkeypatch.setattr(limiters.shutil, "which", lambda _tool: "/usr/bin/tuned-adm")
    missing = limiters.controls_status(run=FakeRun(), exists=lambda _p: False)
    assert missing["available"] is False and missing["reason"] == "not installed"

    denied = limiters.controls_status(run=FakeRun(returncode=1), exists=lambda _p: True)
    assert denied["available"] is False and "sudo" in denied["reason"]

    run = FakeRun(stdout="ok\n")
    ready = limiters.controls_status(run=run, exists=lambda _p: True)
    assert ready["available"] is True and ready["profiles"] == ["balanced", "powersave"]
    assert run.calls[0] == ["sudo", "-n", limiters.DEFAULT_HELPER, "check"]


def test_apply_setting_validates_then_calls_the_helper(monkeypatch):
    monkeypatch.setenv(limiters.HELPER_ENV, "/opt/helper")
    run = FakeRun(stdout="GPU power limit set to 230 W and kept for future boots\n")

    assert limiters.apply_setting("gpu_power", "230", run=run) == (
        True,
        "GPU power limit set to 230 W and kept for future boots",
    )
    assert run.calls[-1] == ["sudo", "-n", "/opt/helper", "gpu-power", "230"]

    assert limiters.apply_setting("profile", "powersave", run=run)[0] is True
    assert run.calls[-1][-2:] == ["profile", "powersave"]

    calls = len(run.calls)
    assert limiters.apply_setting("gpu_power", "250;id", run=run)[0] is False
    assert limiters.apply_setting("profile", "../etc", run=run)[0] is False
    assert limiters.apply_setting("fans", "max", run=run)[0] is False
    assert len(run.calls) == calls  # nothing invalid reaches sudo

    failing = FakeRun(
        returncode=1, stderr="fedora-debugg-limiter: 999 W is outside this GPU's range\n"
    )
    assert limiters.apply_setting("gpu_power", "999", run=failing) == (
        False,
        "999 W is outside this GPU's range",
    )


def test_page_offers_controls_only_when_available(tmp_path):
    sys_class, cpu, etc = _fake_host(tmp_path)

    def reader():
        return limiters.read_limiters(sys_class, cpu_dir=cpu, etc=etc, gpu_query=lambda: GPU_CSV)

    root = tmp_path / "tacho"
    thermal.append_sample(
        thermal.samples_path(root),
        thermal.collect_sample(
            sys_class, now=1000, gpu_reader=lambda: 60.0, limits_reader=reader,
            gpu_fan_reader=lambda: None, platform="linux",
        ),
    )  # fmt: skip

    off = thermal.render_page(
        root, "7d", now=1100, controls={"available": False, "reason": "not installed"}
    )
    assert "install_limiter_controls.sh" in off and 'action="/thermal/limiters"' not in off

    on = thermal.render_page(
        root,
        "7d",
        now=1100,
        controls={"available": True, "profiles": ["balanced", "powersave"]},
        message="Power profile set to <powersave>",
    )
    assert 'name="value" min="100" max="450" step="5" value="250"' in on
    assert "<option selected>balanced</option><option>powersave</option>" in on
    assert 'name="range" value="7d"' in on
    assert "Power profile set to &lt;powersave&gt;" in on


# --- disks --------------------------------------------------------------------


def _block(root: Path) -> Path:
    block = root / "sys" / "block"
    for dev, rotational, scheduler, read_ahead, model in (
        ("nvme0n1", "0", "[none] mq-deadline kyber bfq", "128", "WD SN750"),
        ("sda", "0", "none mq-deadline kyber [bfq]", "128", "Samsung SSD 870"),
        ("sdb", "1", "none mq-deadline kyber [bfq]", "8192", "ST16000NT001"),
        ("loop0", "0", "[none]", "128", ""),
    ):
        _write(block / dev / "queue" / "rotational", rotational)
        _write(block / dev / "queue" / "scheduler", scheduler)
        _write(block / dev / "queue" / "read_ahead_kb", read_ahead)
        _write(block / dev / "device" / "model", f"{model}   ")
        _write(block / dev / "size", "31251759104")
    return block


def test_disk_limits_report_scheduler_read_ahead_and_type(tmp_path):
    disks = {d["dev"]: d for d in limiters.disk_limits(_block(tmp_path))}

    assert set(disks) == {"nvme0n1", "sda", "sdb"}  # loop devices are not disks
    assert disks["sdb"] == {
        "dev": "sdb",
        "model": "ST16000NT001",
        "kind": "hdd",
        "scheduler": "bfq",
        "read_ahead_kb": 8192,
    }
    assert disks["nvme0n1"]["kind"] == "nvme" and disks["nvme0n1"]["scheduler"] == "none"

    rows = {r["limiter"]: r for r in limiters.describe({"disks": list(disks.values())})}
    ssd = rows["Disk I/O — sda Samsung SSD 870 (SSD)"]
    assert ssd["setting"] == "bfq, 128 KiB read-ahead"
    assert ssd["state"] == "mq-deadline suits SSD better" and ssd["light"] == "yellow"
    assert rows["Disk I/O — sdb ST16000NT001 (HDD)"]["light"] == "green"


def test_disk_counters_become_busy_percent_and_throughput(tmp_path):
    stats = tmp_path / "diskstats"
    stats.write_text(
        "   8  16 sdb 100 0 2000 0 50 0 4000 0 0 1000 0 0 0 0 0\n"
        "   8  17 sdb1 100 0 2000 0 50 0 4000 0 0 1000 0 0 0 0 0\n"
        "   7   0 loop0 1 0 8 0 0 0 0 0 0 1 0 0 0 0 0\n"
        " 259   0 nvme0n1 5 0 80 0 5 0 80 0 0 10 0 0 0 0 0\n",
        encoding="utf-8",
    )
    counters = thermal._read_disk_counters(stats)
    assert counters == {"sdb": [2000, 4000, 1000], "nvme0n1": [80, 80, 10]}

    before = {"ts": 1000, "disk_counters": counters}
    after = {
        "ts": 1300,
        "disk_counters": {
            "sdb": [2000 + 300 * 2048, 4000 + 300 * 4096, 1000 + 150_000],  # 1 + 2 MiB/s, 50% busy
            "nvme0n1": [0, 0, 0],  # counters reset: rebooted
        },
    }
    rates = thermal.disk_rates(before, after)
    assert rates == {"sdb": {"busy": 50.0, "read_mbs": 1.05, "write_mbs": 2.1}}
    assert thermal.disk_rates(None, after) == {}
    assert thermal.disk_rates({"ts": 1300, "disk_counters": counters}, after) == {}


def test_disk_activity_reaches_payload_hourly_history_and_page(tmp_path):
    root = tmp_path / "tacho"
    path = thermal.samples_path(root)
    thermal.append_sample(path, {"ts": 1000, "disk_counters": {"sdb": [0, 0, 0]}})
    thermal.append_sample(path, {"ts": 1300, "disk_counters": {"sdb": [0, 300 * 2048, 30_000]}})

    assert thermal.load_samples(path)[-1]["disks"]["sdb"]["busy"] == 10.0
    payload = thermal.build_payload(root, "24h", now=1400)
    assert payload["samples"][-1]["disk_busy:sdb"] == 10.0
    assert payload["samples"][-1]["disk_mbs:sdb"] == 1.05
    assert "sdb" in payload["disk_labels"]

    points = thermal.hourly_series(
        [
            {"ts": 3600, "disks": {"sdb": {"busy": 5.0, "read_mbs": 1.0, "write_mbs": 0.0}}},
            {"ts": 3900, "disks": {"sdb": {"busy": 40.0, "read_mbs": 9.0, "write_mbs": 1.0}}},
        ],
        {},
        since=0,
    )
    assert points[0]["disk_busy:sdb"] == 40.0 and points[0]["disk_mbs:sdb"] == 10.0

    page = thermal.render_page(root, "24h", now=1400, controls={"available": False})
    assert "<h2>Disks</h2>" in page


def test_disk_labels_name_model_size_and_type(tmp_path):
    labels = thermal.disk_labels(["sdb", "nvme0n1"], _block(tmp_path))
    assert labels["sdb"] == "sdb · ST16000NT001 · 16 TB HDD"
    assert labels["nvme0n1"] == "nvme0n1 · WD SN750 · 16 TB NVMe"


def test_disk_rows_include_the_volumes_reads_go_through(tmp_path):
    block = _block(tmp_path)
    dm = tmp_path / "sys" / "devices" / "virtual" / "block" / "dm-2"
    _write(dm / "dm" / "name", "4tb-m2")
    _write(dm / "queue" / "read_ahead_kb", "512")
    (block / "nvme0n1" / "holders").mkdir(parents=True)
    (block / "nvme0n1" / "holders" / "dm-2").symlink_to(dm)
    part = block / "sda" / "sda1" / "holders"
    part.mkdir(parents=True)
    dm1 = tmp_path / "sys" / "devices" / "virtual" / "block" / "dm-1"
    _write(dm1 / "dm" / "name", "4tb-sata")
    _write(dm1 / "queue" / "read_ahead_kb", "128")
    (part / "dm-1").symlink_to(dm1)

    disks = {d["dev"]: d for d in limiters.disk_limits(block)}

    assert disks["nvme0n1"]["volumes"] == [{"name": "4tb-m2", "read_ahead_kb": 512}]
    assert disks["sda"]["volumes"] == [{"name": "4tb-sata", "read_ahead_kb": 128}]
    assert "volumes" not in disks["sdb"]
    rows = {r["limiter"]: r for r in limiters.describe({"disks": list(disks.values())})}
    assert rows["Disk I/O — nvme0n1 WD SN750 (NVMe)"]["setting"] == (
        "none, 128 KiB read-ahead; volume 4tb-m2 512 KiB"
    )
