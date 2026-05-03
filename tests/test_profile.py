from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from dyno_lab import ProcessRecorder, SubprocessPatch, TempWorkdir, build_completed_process

from tachometer.profile import (
    _count_lines,
    _gpu_snapshot,
    append_profile_sample,
    collect_host_resource_snapshot,
    collect_resource_snapshot,
    run_profiled_command,
    summarize_delta_pairs,
    summarize_run_records,
    summarize_samples,
)


@pytest.mark.unit
def test_gpu_snapshot_returns_gpu_detected_false_when_nvidia_smi_missing():
    with patch("shutil.which", return_value=None):
        result = _gpu_snapshot()

    assert result == {"gpu_detected": False}


@pytest.mark.unit
def test_gpu_snapshot_parses_valid_csv():
    csv_output = "NVIDIA GeForce RTX 3080, 45, 8192, 10240, 72, 35\n"
    recorder = ProcessRecorder(responses=[build_completed_process(stdout=csv_output)])

    with (
        patch("shutil.which", return_value="/usr/bin/nvidia-smi"),
        SubprocessPatch(recorder),
    ):
        result = _gpu_snapshot()

    assert recorder.call_count == 1
    assert result["gpu_detected"] is True
    assert result["gpu_name"] == "NVIDIA GeForce RTX 3080"
    assert result["gpu_util_percent"] == 45.0
    assert result["gpu_mem_used_mb"] == 8192.0
    assert result["gpu_mem_total_mb"] == 10240.0
    assert result["gpu_temp_celsius"] == 72.0
    assert result["gpu_fan_pct"] == 35.0


@pytest.mark.unit
def test_count_lines_classifies_source_doc_and_config():
    with TempWorkdir() as wd:
        wd.populate(
            {
                "src/main.py": "def hello():\n    pass\n",
                "src/utils.py": "import os\n",
                "scripts/setup.sh": "#!/bin/bash\necho hi\n",
                "README.md": "# Title\n\nSome docs.\n",
                "docs/guide.rst": "Guide\n=====\n",
                "pyproject.toml": "[project]\nname = 'x'\n",
                "config.yml": "key: value\n",
                ".venv/lib/skip.py": "should_be_skipped\n",
                "__pycache__/cached.pyc": "bytecode",
                "data.lock": "lock content\n",
            }
        )
        result = _count_lines(wd.path)

    assert result["source_lines"] == 5  # main.py(2) + utils.py(1) + setup.sh(2)
    assert result["doc_lines"] == 5  # README.md(3) + guide.rst(2)
    assert result["config_lines"] == 3  # pyproject.toml(2) + config.yml(1)


@pytest.mark.unit
def test_count_lines_skips_files_over_size_limit(tmp_path: Path):
    big = tmp_path / "huge.py"
    big.write_bytes(b"x = 1\n" * 100_000)  # ~600 KB, over the 512 KB limit
    small = tmp_path / "tiny.py"
    small.write_text("y = 2\n", encoding="utf-8")

    result = _count_lines(tmp_path)

    assert result["source_lines"] == 1


@pytest.mark.integration
def test_collect_resource_snapshot_includes_repo_metrics_for_git_repo(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repo_root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

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


@pytest.mark.integration
def test_collect_host_resource_snapshot_excludes_repo_metrics(tmp_path: Path):
    snapshot = collect_host_resource_snapshot(path=tmp_path)

    assert snapshot.repo_root is None
    assert snapshot.repo_file_count is None
    assert snapshot.repo_size_bytes is None


@pytest.mark.unit
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


@pytest.mark.unit
def test_summarize_samples_includes_loc_fields(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    append_profile_sample(
        profile_path,
        {"source_lines": 1000, "doc_lines": 200, "config_lines": 50},
    )
    append_profile_sample(
        profile_path,
        {"source_lines": 1100, "doc_lines": 210, "config_lines": 60},
    )

    summary = summarize_samples(profile_path)

    assert summary["latest_source_lines"] == 1100
    assert summary["latest_doc_lines"] == 210
    assert summary["latest_config_lines"] == 60


@pytest.mark.slow
@pytest.mark.integration
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


@pytest.mark.unit
def test_summarize_delta_pairs_no_file(tmp_path: Path):
    result = summarize_delta_pairs(tmp_path / "missing.json")
    assert result == {"pair_count": 0}


@pytest.mark.unit
def test_summarize_delta_pairs_no_pairs(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"samples": [{"cpu_percent": 10.0}], "runs": []}),
        encoding="utf-8",
    )
    result = summarize_delta_pairs(profile_path)
    assert result == {"pair_count": 0}


@pytest.mark.unit
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


@pytest.mark.unit
def test_summarize_delta_pairs_unmatched_pre_ignored(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    samples = [
        {"name": "test", "phase": "pre", "cpu_percent": 5.0},
        # no matching post
    ]
    profile_path.write_text(json.dumps({"samples": samples, "runs": []}), encoding="utf-8")
    result = summarize_delta_pairs(profile_path)
    assert result == {"pair_count": 0}


@pytest.mark.unit
def test_summarize_run_records_no_file(tmp_path: Path):
    result = summarize_run_records(tmp_path / "missing.json")
    assert result["run_count"] == 0
    assert result["qualifying_run_count"] == 0


@pytest.mark.unit
def test_summarize_run_records_no_psutil_data(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"samples": [], "runs": [{"returncode": 0}]}),
        encoding="utf-8",
    )
    result = summarize_run_records(profile_path)
    assert result["run_count"] == 1
    assert result["qualifying_run_count"] == 0


@pytest.mark.unit
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
