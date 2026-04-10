from __future__ import annotations

import json
import sys
from pathlib import Path

from tachometer.cli import main


def _write_manifest(repo_root: Path, *, name: str = "temp", category: str = "util-repos") -> Path:
    manifest_dir = repo_root / "config" / "tachometer"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "profile.toml"
    manifest_path.write_text(
        "\n".join(
            [
                "[repo]",
                f'name = "{name}"',
                f'category = "{category}"',
                'kind = "python"',
                "",
                "[paths]",
                'disk_path = "."',
                'profile_path = ".tachometer/profile.json"',
                'summary_path = ".tachometer/summary.json"',
                "",
                "[defaults]",
                'label = "repo-snapshot"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def test_cli_snapshot_writes_profile_and_summary(tmp_path: Path, capsys):
    manifest_path = _write_manifest(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    rc = main(["snapshot", "--manifest", str(manifest_path)])

    assert rc == 0
    assert (tmp_path / ".tachometer" / "profile.json").exists()
    assert (tmp_path / ".tachometer" / "summary.json").exists()
    output = json.loads(capsys.readouterr().out)
    assert output["name"] == "repo-snapshot"


def test_cli_host_snapshot_writes_host_profile_and_summary(tmp_path: Path, capsys):
    manifest_path = _write_manifest(tmp_path)

    rc = main(["host-snapshot", "--manifest", str(manifest_path)])

    assert rc == 0
    assert (tmp_path / ".tachometer" / "host-profile.json").exists()
    assert (tmp_path / ".tachometer" / "host-summary.json").exists()
    output = json.loads(capsys.readouterr().out)
    assert output["name"] == "host-snapshot"
    assert output["repo_root"] is None


def test_cli_run_profiles_command(tmp_path: Path):
    manifest_path = _write_manifest(tmp_path, name="doseido", category="health-repos")

    rc = main(
        [
            "run",
            "--manifest",
            str(manifest_path),
            "--name",
            "smoke",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        ]
    )

    profile = json.loads((tmp_path / ".tachometer" / "profile.json").read_text(encoding="utf-8"))
    summary = json.loads((tmp_path / ".tachometer" / "summary.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert len(profile["samples"]) == 2
    assert len(profile["runs"]) == 1
    assert summary["sample_count"] == 2
