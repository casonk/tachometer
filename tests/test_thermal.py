"""Tests for the host thermal sampler, its history file, and the chart page."""

from __future__ import annotations

import json
from pathlib import Path

from tachometer import thermal


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _fake_sysfs(root: Path) -> Path:
    _write(root / "thermal/thermal_zone0/type", "acpitz")
    _write(root / "thermal/thermal_zone0/temp", "27800")
    _write(root / "thermal/thermal_zone1/type", "x86_pkg_temp")
    _write(root / "thermal/thermal_zone1/temp", "91000")
    _write(root / "hwmon/hwmon0/name", "nvme")
    _write(root / "hwmon/hwmon0/temp1_input", "66850")
    _write(root / "hwmon/hwmon1/name", "drivetemp")
    _write(root / "hwmon/hwmon1/temp1_input", "58000")
    _write(root / "hwmon/hwmon2/name", "nct6798")
    _write(root / "hwmon/hwmon2/temp1_label", "SYSTIN")
    _write(root / "hwmon/hwmon2/temp1_input", "68000")
    _write(root / "hwmon/hwmon2/temp2_label", "CPUTIN")
    _write(root / "hwmon/hwmon2/temp2_input", "80000")
    _write(root / "hwmon/hwmon2/fan1_input", "1264")
    _write(root / "hwmon/hwmon2/fan2_input", "2146")
    return root


def test_collect_sample_reads_each_sensor_from_the_right_place(tmp_path):
    sample = thermal.collect_sample(_fake_sysfs(tmp_path), now=1000, gpu_reader=lambda: 88.0)

    assert sample["ts"] == 1000
    assert sample["cpu"] == 91.0  # the package zone, not the 27.8 C board zone
    assert sample["board"] == 68.0  # SYSTIN, not CPUTIN
    assert sample["gpu"] == 88.0
    assert sample["fan_max"] == 2146
    assert sorted(sample["drives"].values()) == [58.0, 66.85]
    assert sample["drive_max"] == 66.85
    assert {key.split(":")[0] for key in sample["drives"]} == {"nvme", "disk"}


def test_collect_sample_reports_missing_sensors_as_none(tmp_path):
    sample = thermal.collect_sample(tmp_path, now=5, gpu_reader=lambda: None)

    assert (sample["cpu"], sample["gpu"], sample["board"], sample["drive_max"]) == (None,) * 4
    assert sample["drives"] == {} and sample["fan_max"] is None


def test_append_sample_keeps_a_rolling_window(tmp_path):
    path = thermal.samples_path(tmp_path)
    day = 86400
    for ts in (0, 5 * day, 13 * day):
        thermal.append_sample(path, {"ts": ts, "cpu": 50.0}, keep_days=14)
    kept = thermal.append_sample(path, {"ts": 16 * day, "cpu": 51.0}, keep_days=14)

    assert kept == 3
    assert [s["ts"] for s in thermal.load_samples(path)] == [5 * day, 13 * day, 16 * day]


def test_load_samples_skips_damaged_lines(tmp_path):
    path = thermal.samples_path(tmp_path)
    _write(path, '{"ts": 10, "cpu": 40}\nnot json\n{"cpu": 41}\n[1, 2]\n{"ts": 20, "cpu": 42}')

    assert [s["ts"] for s in thermal.load_samples(path)] == [10, 20]
    assert [s["ts"] for s in thermal.load_samples(path, since=15)] == [20]
    assert thermal.load_samples(tmp_path / "absent.jsonl") == []


def test_lights_follow_the_limit_for_each_kind():
    assert thermal.light_for("cpu", 79.9) == "green"
    assert thermal.light_for("cpu", 80.0) == "yellow"
    assert thermal.light_for("cpu", 98.0) == "red"
    assert thermal.light_for("gpu", None) == "unknown"
    # 66 C is fine for an NVMe drive and hot for a SATA disk.
    assert thermal.drive_light({"nvme:nvme0": 66.0}) == "green"
    assert thermal.drive_light({"nvme:nvme0": 66.0, "disk:sda": 66.0}) == "red"
    assert thermal.drive_light({}) == "unknown"


def test_latest_status_takes_the_worst_light_and_reports_age():
    samples = [
        {"ts": 100, "cpu": 40.0, "gpu": 40.0, "board": 30.0, "drives": {}},
        {
            "ts": 400,
            "cpu": 98.0,
            "gpu": 60.0,
            "board": 58.0,
            "drive_max": 48.0,
            "drives": {"nvme:nvme0": 48.0},
        },
    ]

    status = thermal.latest_status(samples, now=460)

    assert status["overall"] == "red"
    assert status["age_seconds"] == 60
    assert [r["light"] for r in status["readings"]] == [
        "red",
        "green",
        "yellow",
        "green",
        "unknown",
    ]
    assert thermal.latest_status([]) == {
        "has_data": False,
        "readings": [],
        "overall": "unknown",
        "age_seconds": None,
    }


def test_payload_windows_samples_and_rejects_unknown_ranges(tmp_path):
    path = thermal.samples_path(tmp_path)
    now = 10 * 86400
    for hours_ago in (200, 30, 5, 1):
        thermal.append_sample(path, {"ts": now - hours_ago * 3600, "cpu": 50.0}, keep_days=30)

    assert len(thermal.build_payload(tmp_path, "6h", now=now)["samples"]) == 2
    assert len(thermal.build_payload(tmp_path, "24h", now=now)["samples"]) == 2
    assert len(thermal.build_payload(tmp_path, "7d", now=now)["samples"]) == 3
    assert thermal.build_payload(tmp_path, "forever", now=now)["range"] == "24h"
    # The status always describes the newest sample, whatever the window.
    assert thermal.build_payload(tmp_path, "6h", now=now)["status"]["age_seconds"] == 3600


def test_page_renders_with_and_without_data_and_escapes_embedded_json(tmp_path):
    empty = thermal.render_page(tmp_path, "24h", now=100)
    assert "No samples yet" in empty and "no samples yet" in empty

    path = thermal.samples_path(tmp_path)
    thermal.append_sample(
        path,
        {
            "ts": 1000,
            "cpu": 91.0,
            "gpu": 50.0,
            "board": 40.0,
            "drive_max": 45.0,
            "drives": {"nvme:nvme0": 45.0},
        },
    )
    page = thermal.render_page(tmp_path, "7d", now=1060)

    assert "91°C" in page and "Hot" in page
    assert 'href="/thermal?range=7d" aria-current="true"' in page
    assert "__" not in page.split("<script>")[0]
    data = page.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(data.replace("<\\/", "</"))["samples"][0]["cpu"] == 91.0


DAY = 86400


def test_expired_samples_are_folded_into_hourly_records_not_lost(tmp_path):
    path = thermal.samples_path(tmp_path)
    base = 100 * DAY  # on an hour boundary
    for offset, cpu in ((0, 40.0), (300, 90.0), (600, 50.0), (3600, 60.0)):
        thermal.append_sample(path, {"ts": base + offset, "cpu": cpu, "load": cpu / 100})
    assert not thermal.hourly_path(tmp_path).exists()

    kept = thermal.append_sample(path, {"ts": base + 31 * DAY, "cpu": 45.0})

    assert kept == 1
    hourly = thermal.load_hourly(thermal.hourly_path(tmp_path))
    assert sorted(hourly) == [base, base + 3600]
    first = hourly[base]
    assert first["n"] == 3
    assert (first["cpu"]["min"], first["cpu"]["max"], first["cpu"]["n"]) == (40.0, 90.0, 3)
    assert first["cpu"]["sum"] == 180.0
    assert hourly[base + 3600]["cpu"]["max"] == 60.0


def test_hourly_records_are_merged_across_appends_and_expire_after_their_own_window(tmp_path):
    path = thermal.samples_path(tmp_path)
    base = 200 * DAY
    thermal.append_sample(path, {"ts": base, "cpu": 40.0})
    thermal.append_sample(path, {"ts": base + 300, "cpu": 70.0})
    # Two later appends each push one of the old samples out of the raw window.
    thermal.append_sample(path, {"ts": base + 30 * DAY + 60, "cpu": 41.0})
    thermal.append_sample(path, {"ts": base + 30 * DAY + 400, "cpu": 42.0})

    bucket = thermal.load_hourly(thermal.hourly_path(tmp_path))[base]
    assert (bucket["n"], bucket["cpu"]["min"], bucket["cpu"]["max"]) == (2, 40.0, 70.0)

    thermal.append_sample(path, {"ts": base + 91 * DAY, "cpu": 43.0})
    assert base not in thermal.load_hourly(thermal.hourly_path(tmp_path))


def test_hourly_series_reports_peak_temperature_and_mean_load():
    hour = 500 * 3600
    raw = [
        {"ts": hour + 60, "cpu": 50.0, "load": 0.2, "fan_max": 900},
        {"ts": hour + 360, "cpu": 96.0, "load": 0.8, "fan_max": 2100},
    ]
    earlier = hour - 3600
    stored = {
        earlier: {
            "ts": hour - 3600,
            "n": 2,
            "cpu": {"min": 30.0, "max": 44.0, "sum": 74.0, "n": 2},
        },
        # A stored bucket for an hour that also has raw samples is merged, not replaced.
        hour: {"ts": hour, "n": 1, "cpu": {"min": 99.0, "max": 99.0, "sum": 99.0, "n": 1}},
    }

    points = thermal.hourly_series(raw, stored, since=hour - 3600)

    assert [p["ts"] for p in points] == [hour - 3600, hour]
    assert points[0]["cpu"] == 44.0 and points[0]["load"] is None
    assert points[1]["cpu"] == 99.0
    assert points[1]["load"] == 0.5
    assert points[1]["fan_max"] == 2100
    assert stored[hour]["n"] == 1, "building a view must not change the stored records"
    assert thermal.hourly_series(raw, stored, since=hour)[0]["ts"] == hour


def test_long_ranges_use_hourly_points_and_short_ranges_stay_raw(tmp_path):
    path = thermal.samples_path(tmp_path)
    now = 400 * DAY
    for age_days in (80, 40, 10, 1):
        for minute in (0, 5):
            thermal.append_sample(
                path, {"ts": now - age_days * DAY + minute * 60, "cpu": 50.0 + minute}
            )

    month = thermal.build_payload(tmp_path, "30d", now=now)
    quarter = thermal.build_payload(tmp_path, "90d", now=now)
    week = thermal.build_payload(tmp_path, "7d", now=now)

    assert (month["resolution"], quarter["resolution"], week["resolution"]) == (
        "hourly",
        "hourly",
        "5min",
    )
    assert len(month["samples"]) == 2  # days 10 and 1, one hour each
    assert len(quarter["samples"]) == 4  # plus the two aged-out days
    assert all(p["cpu"] == 55.0 for p in quarter["samples"])  # hourly peak
    assert len(week["samples"]) == 2  # the two raw samples from yesterday


def test_page_labels_hourly_ranges_as_such(tmp_path):
    path = thermal.samples_path(tmp_path)
    thermal.append_sample(path, {"ts": 1000, "cpu": 50.0, "load": 0.1})

    page = thermal.render_page(tmp_path, "90d", now=1060)

    assert "hourly maximum" in page and "(hourly mean)" in page
    assert 'href="/thermal?range=30d"' in page
    assert "__" not in page.split("<script>")[0]
    assert "hourly maximum" not in thermal.render_page(tmp_path, "24h", now=1060)


def _smart_file(directory: Path, name: str, *snapshots: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in snapshots), encoding="utf-8"
    )


def _snap(when: str, *, temp=40, serial="S1", kind="ata", passed=True, **counters) -> dict:
    return {
        "taken_at": when,
        "kind": kind,
        "serial": serial,
        "health_passed": passed,
        "temperature_c": temp,
        "counters": counters,
    }


NOW = 1791720000.0  # 2026-10-11T12:40:00+00:00


def test_read_smart_takes_the_latest_fresh_snapshot_and_flags_rising_counters(tmp_path):
    smart = tmp_path / "smart"
    _smart_file(
        smart,
        "ssd",
        _snap("2026-10-11T11:00:00+00:00", temp=58, bad_blocks=1187),
        _snap("2026-10-11T12:00:00+00:00", temp=71, bad_blocks=1188),
    )
    _smart_file(smart, "nvme", _snap("2026-10-11T12:10:00+00:00", temp=67, kind="nvme"))
    _smart_file(smart, "stale", _snap("2026-10-10T01:00:00+00:00", temp=99))
    _smart_file(
        smart,
        "swapped",
        _snap("2026-10-11T11:00:00+00:00", serial="OLD", errors=50),
        _snap("2026-10-11T12:00:00+00:00", serial="NEW", errors=60),
    )
    (smart / "broken.jsonl").write_text("not json\n", encoding="utf-8")

    result = thermal.read_smart(smart, now=NOW)

    assert sorted(result) == ["nvme", "ssd", "swapped"]
    assert result["ssd"]["temp"] == 71.0 and result["ssd"]["rose"] == ["bad_blocks"]
    assert result["ssd"]["kind"] == "disk" and result["nvme"]["kind"] == "nvme"
    assert result["swapped"]["rose"] == [], "a replaced drive starts a new trend"
    assert thermal.read_smart(tmp_path / "absent", now=NOW) == {}


def test_collect_sample_merges_smart_temperatures_with_kernel_readings(tmp_path):
    smart = tmp_path / "smart"
    _smart_file(smart, "ssd", _snap("2026-10-11T12:30:00+00:00", temp=71))
    sysfs = _fake_sysfs(tmp_path / "sys")

    sample = thermal.collect_sample(sysfs, now=NOW, gpu_reader=lambda: None, smart_dir=smart)

    assert sample["drives"]["smart-disk:ssd"] == 71.0
    assert sample["drive_max"] == 71.0  # hotter than either kernel reading
    assert thermal.drive_light(sample["drives"]) == "red"
    # 71 C through SMART on an NVMe drive would only be a "watch".
    assert thermal.drive_light({"smart-nvme:x": 71.0}) == "yellow"


def test_drive_health_light_and_reason():
    ok = {"a": {"health_passed": True, "rose": []}}
    assert thermal.drive_health({}) == {"light": "unknown", "detail": "No SMART data"}
    assert thermal.drive_health(ok)["light"] == "green"
    rising = ok | {"b": {"health_passed": True, "rose": ["pending_sectors"]}}
    assert thermal.drive_health(rising) == {"light": "yellow", "detail": "Fault counter rose: b"}
    failing = rising | {"c": {"health_passed": False, "rose": []}}
    assert thermal.drive_health(failing) == {"light": "red", "detail": "SMART failed: c"}


def test_drive_health_reaches_the_status_and_the_page(tmp_path):
    path = thermal.samples_path(tmp_path)
    thermal.append_sample(
        path,
        {
            "ts": 1000,
            "cpu": 40.0,
            "drives": {},
            "smart": {"ssd": {"health_passed": True, "rose": ["bad_blocks"], "kind": "disk"}},
        },
    )

    status = thermal.build_payload(tmp_path, "24h", now=1060)["status"]
    assert status["readings"][-1]["label"] == "Drive health"
    assert status["readings"][-1]["light"] == "yellow" and status["overall"] == "yellow"

    page = thermal.render_page(tmp_path, "24h", now=1060)
    assert "Drive health" in page and "Fault counter rose: ssd" in page
