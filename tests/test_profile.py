from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from tachometer.profile import (
    _gpu_snapshot,
    append_profile_sample,
    collect_host_resource_snapshot,
    collect_resource_snapshot,
    run_profiled_command,
    summarize_delta_pairs,
    summarize_run_records,
    summarize_samples,
)


def test_gpu_snapshot_returns_gpu_detected_false_when_nvidia_smi_missing():
    with patch("shutil.which", return_value=None):
        result = _gpu_snapshot()

    assert result == {"gpu_detected": False}


def test_gpu_snapshot_parses_valid_csv():
    csv_output = "NVIDIA GeForce RTX 3080, 45, 8192, 10240\n"
    completed = type("Completed", (), {"returncode": 0, "stdout": csv_output})

    with (
        patch("shutil.which", return_value="/usr/bin/nvidia-smi"),
        patch("subprocess.run", return_value=completed),
    ):
        result = _gpu_snapshot()

    assert result["gpu_detected"] is True
    assert result["gpu_name"] == "NVIDIA GeForce RTX 3080"
    assert result["gpu_util_percent"] == 45.0
    assert result["gpu_mem_used_mb"] == 8192.0
    assert result["gpu_mem_total_mb"] == 10240.0


def test_collect_resource_snapshot_includes_repo_metrics_for_git_repo(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repo_root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    import subprocess

    subprocess.run(["git", "init", str(repo_root)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo_root), "add", "tracked.txt"], check=True)

    snapshot = collect_resource_snapshot(path=repo_root, repo_root=repo_root)

    assert snapshot.repo_root == str(repo_root.resolve())
    assert snapshot.repo_file_count is not None
    assert snapshot.repo_file_count >= 2
    assert snapshot.repo_size_bytes is not None
    assert snapshot.repo_size_bytes > 0
    assert snapshot.git_tracked_file_count == 1
    assert snapshot.git_untracked_file_count is not None
    assert snapshot.git_untracked_file_count >= 1


def test_collect_host_resource_snapshot_excludes_repo_metrics(tmp_path: Path):
    snapshot = collect_host_resource_snapshot(path=tmp_path)

    assert snapshot.repo_root is None
    assert snapshot.repo_file_count is None
    assert snapshot.repo_size_bytes is None


def test_append_and_summarize_samples(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    append_profile_sample(
        profile_path,
        {"cpu_percent": 20.0, "memory_used_bytes": 100, "repo_size_bytes": 10},
    )
    append_profile_sample(
        profile_path,
        {"cpu_percent": 40.0, "memory_used_bytes": 300, "repo_size_bytes": 30},
    )

    summary = summarize_samples(profile_path)

    assert summary["sample_count"] == 2
    assert summary["avg_cpu_percent"] == 30.0
    assert summary["avg_memory_used_bytes"] == 200.0
    assert summary["avg_repo_size_bytes"] == 20.0
    assert summary["latest_repo_size_bytes"] == 30


def test_run_profiled_command_writes_samples_and_run_record(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    profile_path = tmp_path / "profile.json"

    record = run_profiled_command(
        name="smoke",
        command=[sys.executable, "-c", "print('ok')"],
        profile_path=profile_path,
        repo_root=repo_root,
        path=repo_root,
        cwd=repo_root,
        repo_metadata={"name": "temp"},
    )

    data = json.loads(profile_path.read_text(encoding="utf-8"))
    assert record["returncode"] == 0
    assert record["summary"]["sample_count"] == 2
    assert len(data["samples"]) == 2
    assert len(data["runs"]) == 1
    assert "ok" in data["runs"][0]["stdout_tail"]


def test_summarize_delta_pairs_no_file(tmp_path: Path):
    result = summarize_delta_pairs(tmp_path / "missing.json")
    assert result == {"pair_count": 0}


def test_summarize_delta_pairs_no_pairs(tmp_path: Path):
    # Snapshot-only profile — all samples lack phase
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"samples": [{"cpu_percent": 10.0}], "runs": []}),
        encoding="utf-8",
    )
    result = summarize_delta_pairs(profile_path)
    assert result == {"pair_count": 0}


def test_summarize_delta_pairs_matched_pairs(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    samples = [
        {"name": "build", "phase": "pre", "cpu_percent": 10.0, "memory_used_bytes": 1000},
        {"name": "build", "phase": "post", "cpu_percent": 30.0, "memory_used_bytes": 1500},
        {"name": "build", "phase": "pre", "cpu_percent": 20.0, "memory_used_bytes": 2000},
        {"name": "build", "phase": "post", "cpu_percent": 50.0, "memory_used_bytes": 3000},
    ]
    profile_path.write_text(json.dumps({"samples": samples, "runs": []}), encoding="utf-8")

    result = summarize_delta_pairs(profile_path)

    assert result["pair_count"] == 2
    # avg delta cpu: ((30-10) + (50-20)) / 2 = 25.0
    assert result["avg_delta_cpu_percent"] == 25.0
    # avg delta memory: ((1500-1000) + (3000-2000)) / 2 = 750.0
    assert result["avg_delta_memory_used_bytes"] == 750.0


def test_summarize_delta_pairs_unmatched_pre_ignored(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    samples = [
        {"name": "test", "phase": "pre", "cpu_percent": 5.0},
        # no matching post
    ]
    profile_path.write_text(json.dumps({"samples": samples, "runs": []}), encoding="utf-8")
    result = summarize_delta_pairs(profile_path)
    assert result == {"pair_count": 0}


def test_summarize_run_records_no_file(tmp_path: Path):
    result = summarize_run_records(tmp_path / "missing.json")
    assert result["run_count"] == 0
    assert result["qualifying_run_count"] == 0


def test_summarize_run_records_no_psutil_data(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"samples": [], "runs": [{"returncode": 0}]}),
        encoding="utf-8",
    )
    result = summarize_run_records(profile_path)
    assert result["run_count"] == 1
    assert result["qualifying_run_count"] == 0


def test_summarize_run_records_aggregates_psutil(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    runs = [
        {
            "returncode": 0,
            "proc_avg_cpu_percent": 20.0,
            "proc_peak_cpu_percent": 40.0,
            "proc_avg_memory_rss_bytes": 100e6,
            "proc_peak_memory_rss_bytes": 200e6,
        },
        {
            "returncode": 0,
            "proc_avg_cpu_percent": 40.0,
            "proc_peak_cpu_percent": 80.0,
            "proc_avg_memory_rss_bytes": 300e6,
            "proc_peak_memory_rss_bytes": 400e6,
        },
    ]
    profile_path.write_text(json.dumps({"samples": [], "runs": runs}), encoding="utf-8")

    result = summarize_run_records(profile_path)

    assert result["run_count"] == 2
    assert result["qualifying_run_count"] == 2
    assert result["avg_proc_cpu_percent"] == 30.0
    assert result["avg_proc_peak_cpu_percent"] == 60.0
    assert result["avg_proc_memory_rss_bytes"] == 200e6
    assert result["avg_proc_peak_memory_rss_bytes"] == 300e6
