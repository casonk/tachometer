from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import RepoManifest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


def _require_str(mapping: dict[str, Any], key: str, *, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _repo_root_for_manifest(manifest_path: Path) -> Path:
    resolved = manifest_path.resolve()
    if len(resolved.parents) < 3:
        raise ValueError(
            "tachometer manifests must live under config/tachometer/profile.toml or a matching two-level path"
        )
    return resolved.parents[2]


def load_manifest(manifest_path: str | Path) -> RepoManifest:
    path = Path(manifest_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    repo = data.get("repo")
    if not isinstance(repo, dict):
        raise ValueError("manifest must define a [repo] table")

    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("[paths] must be a table when present")

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("[defaults] must be a table when present")

    repo_root = _repo_root_for_manifest(path)
    disk_path = repo_root / paths.get("disk_path", ".")
    profile_path = repo_root / paths.get("profile_path", ".tachometer/profile.json")
    summary_path = repo_root / paths.get("summary_path", ".tachometer/summary.json")

    return RepoManifest(
        name=_require_str(repo, "name", context="repo"),
        category=_require_str(repo, "category", context="repo"),
        kind=_require_str(repo, "kind", context="repo"),
        repo_root=repo_root,
        disk_path=disk_path.resolve(),
        profile_path=profile_path.resolve(),
        summary_path=summary_path.resolve(),
        default_label=str(defaults.get("label", "repo-snapshot")),
    )
