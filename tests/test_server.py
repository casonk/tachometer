from __future__ import annotations

import json
from pathlib import Path

from tachometer.server import (
    _build_api_payload,
    _render_dashboard,
    gather_fedora_debug_data,
    gather_host_data,
)


def test_gather_host_data_loads_canonical_summary(tmp_path: Path):
    host_summary_path = tmp_path / "host-summary.json"
    host_summary_path.write_text(
        json.dumps(
            {
                "sample_count": 3,
                "avg_cpu_percent": 42.0,
                "avg_memory_used_bytes": 6_000_000_000,
                "latest_memory_total_bytes": 12_000_000_000,
                "avg_disk_used_bytes": 200_000_000_000,
                "latest_disk_total_bytes": 400_000_000_000,
                "avg_gpu_util_percent": 11.0,
            }
        ),
        encoding="utf-8",
    )

    host = gather_host_data(host_summary_path)

    assert host["has_data"] is True
    assert host["stoplight_host"]["metrics"]["disk_utilization_ratio"] == 0.5
    assert "repo_size" not in host["stoplight_host"]["lights"]


def test_build_api_payload_includes_host_summary():
    repos = [
        {
            "name": "doseido",
            "category": "health-repos",
            "has_data": True,
            "has_delta": False,
            "has_process": False,
            "stoplight_system": {"overall_light": "green"},
            "stoplight_delta": {},
            "stoplight_process": {},
            "backlog_open": 0,
            "backlog": {},
        }
    ]
    host = {
        "has_data": True,
        "summary": {"sample_count": 1},
        "stoplight_host": {"overall_light": "yellow"},
    }

    fedora_debug = {
        "has_data": True,
        "signals": {"overall_light": "red"},
        "overall_light": "red",
    }

    payload = _build_api_payload(repos, host, fedora_debug)

    assert payload["portfolio_light"] == "green"
    assert payload["host_light"] == "yellow"
    assert payload["host"]["summary"]["sample_count"] == 1
    assert payload["fedora_debug_light"] == "red"


def test_render_dashboard_includes_host_metrics():
    repos = [
        {
            "name": "doseido",
            "category": "health-repos",
            "has_data": False,
            "has_delta": False,
            "has_process": False,
            "stoplight_system": {},
            "stoplight_delta": {},
            "stoplight_process": {},
        }
    ]
    host = {
        "has_data": True,
        "summary": {"sample_count": 2},
        "stoplight_host": {
            "overall_light": "green",
            "metrics": {
                "cpu_percent": 12.0,
                "memory_utilization_ratio": 0.5,
                "disk_utilization_ratio": 0.25,
                "gpu_util_percent": 8.0,
            },
            "lights": {
                "cpu": "green",
                "memory": "green",
                "disk": "green",
                "gpu": "green",
            },
        },
    }
    fedora_debug = {
        "has_data": True,
        "signals": {
            "latest_snapshot_epoch": 4_102_444_800,
            "buckets": {
                "collection": {
                    "label": "Collection",
                    "summary": "2 command failures",
                    "light": "yellow",
                },
                "display": {
                    "label": "Display",
                    "summary": "1 instability markers",
                    "light": "yellow",
                },
                "coredumps": {
                    "label": "Coredumps",
                    "summary": "history 2",
                    "light": "yellow",
                },
                "gpu": {
                    "label": "GPU",
                    "summary": "18 fault markers",
                    "light": "red",
                },
                "storage": {
                    "label": "Storage",
                    "summary": "btrfs counters",
                    "light": "yellow",
                },
                "packages": {
                    "label": "Packages",
                    "summary": "rpm 2200 / flatpak 8 / snap 1",
                    "light": "green",
                },
                "python": {
                    "label": "Python",
                    "summary": "240 pkgs / 12 envs",
                    "light": "green",
                },
                "node": {
                    "label": "Node",
                    "summary": "18 global / 4 proj",
                    "light": "green",
                },
                "go": {
                    "label": "Go",
                    "summary": "120 mods / 3 roots",
                    "light": "green",
                },
            },
        },
        "overall_light": "red",
    }

    html = _render_dashboard(repos, host, fedora_debug, port=5100)

    assert "Portfolio system aggregate" in html
    assert "Host" in html
    assert "Disk" in html
    assert "25.0%" in html
    assert "Fedora Debug" in html
    assert "Collection" in html
    assert "Display" in html
    assert "Coredumps" in html
    assert "Storage" in html
    assert "Packages" in html
    assert "Python" in html
    assert "Node" in html
    assert "Go" in html


def test_render_dashboard_keeps_legacy_fedora_debug_sidecar_shape():
    repos = []
    host = {
        "has_data": False,
        "summary": {},
        "stoplight_host": {},
    }
    fedora_debug = {
        "has_data": True,
        "signals": {
            "latest_snapshot_epoch": 4_102_444_800,
            "metrics": {
                "journal_warning_count": 7,
                "current_coredump_marker_count": 1,
                "gpu_driver_alert": True,
            },
            "lights": {
                "warnings": "yellow",
                "coredumps": "red",
                "gpu": "red",
            },
        },
        "overall_light": "red",
    }

    html = _render_dashboard(repos, host, fedora_debug, port=5100)

    assert "Warnings" in html
    assert "Coredumps" in html
    assert "GPU" in html


def test_gather_fedora_debug_data_loads_sidecar(tmp_path: Path):
    sidecar_path = tmp_path / "tachometer-signals.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "overall_light": "yellow",
                "buckets": {
                    "storage": {
                        "label": "Storage",
                        "summary": "btrfs counters",
                        "light": "yellow",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    fedora_debug = gather_fedora_debug_data(sidecar_path)

    assert fedora_debug["has_data"] is True
    assert fedora_debug["overall_light"] == "yellow"
    assert fedora_debug["signals"]["buckets"]["storage"]["summary"] == "btrfs counters"
