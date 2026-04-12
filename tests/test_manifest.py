from pathlib import Path

from tachometer.manifest import load_manifest


def test_load_example_manifest_resolves_repo_root_and_paths():
    expected_repo_root = Path(__file__).resolve().parent.parent
    manifest = load_manifest(expected_repo_root / "examples" / "doseido" / "repo-profile.toml")

    assert manifest.name == "doseido"
    assert manifest.category == "health-repos"
    assert manifest.kind == "python"
    assert manifest.repo_root == expected_repo_root
    assert manifest.profile_path == manifest.repo_root / ".tachometer" / "profile.json"
    assert manifest.summary_path == manifest.repo_root / ".tachometer" / "summary.json"
    assert manifest.host_profile_path == manifest.repo_root / ".tachometer" / "host-profile.json"
    assert manifest.host_summary_path == manifest.repo_root / ".tachometer" / "host-summary.json"
