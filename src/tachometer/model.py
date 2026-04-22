from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoManifest:
    name: str
    category: str
    kind: str
    repo_root: Path
    disk_path: Path
    profile_path: Path
    summary_path: Path
    host_profile_path: Path
    host_summary_path: Path
    default_label: str = "repo-snapshot"
    notify_shock_relay_root: str | None = None
    notify_service: str | None = None
    notify_target: str | None = None
    notify_config_path: str | None = None

    def repo_metadata(self) -> dict[str, str]:
        return {
            "name": self.name,
            "category": self.category,
            "kind": self.kind,
            "root": str(self.repo_root),
        }


@dataclass
class ResourceSnapshot:
    timestamp: float
    cpu_percent: float | None = None
    loadavg_1m: float | None = None
    loadavg_5m: float | None = None
    loadavg_15m: float | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    memory_used_bytes: int | None = None
    disk_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    disk_free_bytes: int | None = None
    disk_io_read_bytes: int | None = None
    disk_io_write_bytes: int | None = None
    swap_total_bytes: int | None = None
    swap_used_bytes: int | None = None
    net_sent_bytes: int | None = None
    net_recv_bytes: int | None = None
    cpu_count: int | None = None
    gpu_detected: bool = False
    gpu_name: str | None = None
    gpu_util_percent: float | None = None
    gpu_mem_used_mb: float | None = None
    gpu_mem_total_mb: float | None = None
    repo_root: str | None = None
    repo_file_count: int | None = None
    repo_dir_count: int | None = None
    repo_size_bytes: int | None = None
    git_tracked_file_count: int | None = None
    git_dirty_file_count: int | None = None
    git_untracked_file_count: int | None = None
    git_tracked_size_bytes: int | None = None
    git_non_ignored_size_bytes: int | None = None
    git_commit_count: int | None = None
    dep_count: int | None = None
    artefact_size_bytes: int | None = None
    uptime_seconds: float | None = None
    hostname: str | None = None
    process_count: int | None = None
    cpu_temp_celsius: float | None = None
