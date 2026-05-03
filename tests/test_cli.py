from __future__ import annotations

import json
from pathlib import Path

import pytest
from dyno_lab import AttrPatch

import tachometer.server as _server_mod
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


@pytest.mark.integration
def test_cli_snapshot_writes_profile_and_summary(tmp_path: Path, dyno_cli):
    manifest_path = _write_manifest(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    result = dyno_cli(main, ["snapshot", "--manifest", str(manifest_path)])

    assert result.exit_code == 0
    assert (tmp_path / ".tachometer" / "profile.json").exists()
    assert (tmp_path / ".tachometer" / "summary.json").exists()
    output = json.loads(result.stdout)
    assert output["name"] == "repo-snapshot"


@pytest.mark.integration
def test_cli_host_snapshot_writes_host_profile_and_summary(tmp_path: Path, dyno_cli):
    manifest_path = _write_manifest(tmp_path)

    result = dyno_cli(main, ["host-snapshot", "--manifest", str(manifest_path)])

    assert result.exit_code == 0
    assert (tmp_path / ".tachometer" / "host-profile.json").exists()
    assert (tmp_path / ".tachometer" / "host-summary.json").exists()
    output = json.loads(result.stdout)
    assert output["name"] == "host-snapshot"
    assert output["repo_root"] is None


@pytest.mark.unit
def test_cli_agent_utilization_writes_sidecar(tmp_path: Path, dyno_cli):
    import tachometer.cli as cli_mod

    manifest_path = _write_manifest(tmp_path)
    fake_snapshot = {
        "captured_at": 1.0,
        "overall_light": "yellow",
        "providers": {
            "codex": {
                "display_name": "Codex",
                "summary": "P4% / S71%",
                "light": "yellow",
            }
        },
    }

    with AttrPatch(cli_mod, collect_agent_utilization=lambda: fake_snapshot):
        result = dyno_cli(main, ["agent-utilization", "--manifest", str(manifest_path)])

    assert result.exit_code == 0
    assert (tmp_path / ".tachometer" / "agent-utilization.json").exists()
    output = json.loads(result.stdout)
    assert output["overall_light"] == "yellow"
    assert output["providers"]["codex"]["summary"] == "P4% / S71%"


@pytest.mark.slow
@pytest.mark.integration
def test_cli_run_profiles_command(tmp_path: Path):
    import sys

    manifest_path = _write_manifest(tmp_path, name="private-repository", category="health-repos")

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


@pytest.mark.unit
def test_cli_serve_uses_loopback_host_by_default(tmp_path: Path):
    manifest_path = _write_manifest(tmp_path)
    captured: dict[str, object] = {}

    def fake_serve(repo_root, *, host, host_summary_path, port, allow_remote):
        captured.update(
            {
                "repo_root": repo_root,
                "host": host,
                "host_summary_path": host_summary_path,
                "port": port,
                "allow_remote": allow_remote,
            }
        )

    with AttrPatch(_server_mod, serve=fake_serve):
        rc = main(["serve", "--manifest", str(manifest_path)])

    assert rc == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5100
    assert captured["allow_remote"] is False


@pytest.mark.unit
def test_cli_serve_allows_explicit_remote_bind(tmp_path: Path):
    manifest_path = _write_manifest(tmp_path)
    captured: dict[str, object] = {}

    def fake_serve(repo_root, *, host, host_summary_path, port, allow_remote):
        captured.update({"host": host, "allow_remote": allow_remote})

    with AttrPatch(_server_mod, serve=fake_serve):
        rc = main(
            [
                "serve",
                "--manifest",
                str(manifest_path),
                "--host",
                "0.0.0.0",
                "--allow-remote",
            ]
        )

    assert rc == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["allow_remote"] is True
