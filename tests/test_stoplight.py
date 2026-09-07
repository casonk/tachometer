from __future__ import annotations

import pytest

from tachometer.stoplight import evaluate_process


@pytest.mark.unit
def test_evaluate_process_ignores_missing_optional_metrics_for_overall() -> None:
    result = evaluate_process(
        {
            "fail_count": 0,
            "avg_proc_cpu_percent": 4.0,
            "avg_proc_peak_cpu_percent": 8.0,
            "avg_proc_memory_rss_bytes": 20_000_000,
            "avg_proc_peak_memory_rss_bytes": 22_000_000,
            "avg_runtime_seconds": 0.01,
            "avg_proc_major_faults": 0.0,
        }
    )

    assert result["lights"]["proc_threads"] == "unknown"
    assert result["lights"]["proc_invol_ctx"] == "unknown"
    assert result["overall_light"] == "green"


@pytest.mark.unit
def test_evaluate_process_active_run_failures_are_red() -> None:
    result = evaluate_process(
        {
            "fail_count": 1,
            "avg_proc_cpu_percent": 4.0,
            "avg_proc_peak_cpu_percent": 8.0,
            "avg_proc_memory_rss_bytes": 20_000_000,
            "avg_proc_peak_memory_rss_bytes": 22_000_000,
            "avg_runtime_seconds": 0.01,
            "avg_proc_major_faults": 0.0,
        }
    )

    assert result["lights"]["run_failures"] == "red"
    assert result["overall_light"] == "red"
