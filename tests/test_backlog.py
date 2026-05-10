from __future__ import annotations

import json
from pathlib import Path

import pytest

from tachometer.backlog import update_backlog


@pytest.mark.unit
def test_disk_backlog_suggests_agentic_archive_remediation(tmp_path: Path) -> None:
    backlog_path = tmp_path / "backlog.json"

    _, newly_opened = update_backlog(
        backlog_path,
        "system",
        {
            "lights": {"disk": "red"},
            "metrics": {"disk_utilization_ratio": 0.93},
        },
    )

    assert len(newly_opened) == 1
    suggestions = newly_opened[0]["suggestions"]
    assert any("disk-pressure agentic remediation" in item for item in suggestions)
    assert any("auto compression/decompression" in item for item in suggestions)

    persisted = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert persisted[0]["id"] == "system.disk"
