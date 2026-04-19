from pathlib import Path

from tachometer.manifest import load_manifest


def test_load_example_manifest_resolves_repo_root_and_paths():
    manifest = load_manifest(
        Path(__file__).resolve().parent.parent / "examples" / "doseido" / "repo-profile.toml"
    )

    assert manifest.name == "doseido"
    assert manifest.category == "health-repos"
    assert manifest.kind == "python"
    assert "tachometer" in manifest.repo_root.name
    assert manifest.profile_path == manifest.repo_root / ".tachometer" / "profile.json"
    assert manifest.summary_path == manifest.repo_root / ".tachometer" / "summary.json"
    assert manifest.host_profile_path == manifest.repo_root / ".tachometer" / "host-profile.json"
    assert manifest.host_summary_path == manifest.repo_root / ".tachometer" / "host-summary.json"
