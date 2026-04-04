from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict

from .manifest import load_manifest
from .profile import (
    append_profile_sample,
    collect_resource_snapshot,
    run_profiled_command,
    summarize_samples,
    write_summary,
)


def _snapshot(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    label = args.label or manifest.default_label
    snapshot = collect_resource_snapshot(path=manifest.disk_path, repo_root=manifest.repo_root)
    payload = {"name": label, "phase": args.phase, **asdict(snapshot)}
    append_profile_sample(
        manifest.profile_path,
        payload,
        repo_metadata=manifest.repo_metadata(),
    )
    summary = summarize_samples(manifest.profile_path)
    write_summary(manifest.summary_path, summary)
    print(json.dumps(payload, indent=2))
    return 0


def _run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("tachometer run requires a command after --")

    record = run_profiled_command(
        name=args.name or manifest.default_label,
        command=command,
        profile_path=manifest.profile_path,
        repo_root=manifest.repo_root,
        path=manifest.disk_path,
        cwd=manifest.repo_root,
        repo_metadata=manifest.repo_metadata(),
    )
    write_summary(manifest.summary_path, record["summary"])
    print(json.dumps(record, indent=2))
    return int(record["returncode"])


def _summarize(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    summary = summarize_samples(manifest.profile_path)
    write_summary(manifest.summary_path, summary)
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared repo and resource profiling helpers")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Collect one repo snapshot")
    snapshot_parser.add_argument("--manifest", required=True)
    snapshot_parser.add_argument("--label", default=None)
    snapshot_parser.add_argument("--phase", default="snapshot")
    snapshot_parser.set_defaults(func=_snapshot)

    run_parser = subparsers.add_parser("run", help="Profile a command with pre/post samples")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--name", default=None)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=_run)

    summarize_parser = subparsers.add_parser("summarize", help="Print the current JSON summary")
    summarize_parser.add_argument("--manifest", required=True)
    summarize_parser.set_defaults(func=_summarize)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)
