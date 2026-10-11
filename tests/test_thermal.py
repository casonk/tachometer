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
    assert [r["light"] for r in status["readings"]] == ["red", "green", "yellow", "green"]
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
