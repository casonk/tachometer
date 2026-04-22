from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict

from .agent_usage import collect_agent_utilization
from .backlog import update_backlog
from .manifest import load_manifest
from .notify import notify_new_red_lights
from .profile import (
    append_profile_sample,
    collect_host_resource_snapshot,
    collect_repo_resource_snapshot,
    run_profiled_command,
    summarize_delta_pairs,
    summarize_run_records,
    summarize_samples,
    write_summary,
)
from .stoplight import evaluate as stoplight_evaluate
from .stoplight import evaluate_delta, evaluate_host, evaluate_process


def _maybe_notify(manifest, newly_opened: list) -> None:
    if (
        newly_opened
        and manifest.notify_shock_relay_root
        and manifest.notify_service
        and manifest.notify_target
    ):
        notify_new_red_lights(
            newly_opened,
            shock_relay_root=manifest.notify_shock_relay_root,
            service=manifest.notify_service,
            target=manifest.notify_target,
            config_path=manifest.notify_config_path,
            repo_name=manifest.name,
        )


def _snapshot(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    label = args.label or manifest.default_label
    snapshot = collect_repo_resource_snapshot(
        path=manifest.disk_path,
        repo_root=manifest.repo_root,
    )
    payload = {"name": label, "phase": args.phase, **asdict(snapshot)}
    append_profile_sample(
        manifest.profile_path,
        payload,
        repo_metadata=manifest.repo_metadata(),
    )
    summary = summarize_samples(manifest.profile_path)
    write_summary(manifest.summary_path, summary)
    backlog_path = manifest.profile_path.parent / "backlog.json"
    _, newly_opened = update_backlog(backlog_path, "system", stoplight_evaluate(summary))
    _maybe_notify(manifest, newly_opened)
    print(json.dumps(payload, indent=2))
    return 0


def _host_snapshot(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    label = args.label or "host-snapshot"
    snapshot = collect_host_resource_snapshot(path=manifest.disk_path)
    payload = {"name": label, "phase": args.phase, **asdict(snapshot)}
    append_profile_sample(manifest.host_profile_path, payload)
    summary = summarize_samples(manifest.host_profile_path)
    write_summary(manifest.host_summary_path, summary)
    host_backlog_path = manifest.host_profile_path.parent / "host-backlog.json"
    _, newly_opened = update_backlog(host_backlog_path, "host", evaluate_host(summary))
    _maybe_notify(manifest, newly_opened)
    print(json.dumps(payload, indent=2))
    return 0


def _agent_utilization(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    summary_path = manifest.repo_root / ".tachometer" / "agent-utilization.json"
    summary = collect_agent_utilization()
    write_summary(summary_path, summary)
    print(json.dumps(summary, indent=2))
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
    backlog_path = manifest.profile_path.parent / "backlog.json"
    _, sys_new = update_backlog(backlog_path, "system", stoplight_evaluate(record["summary"]))
    delta_new: list = []
    delta_summary = summarize_delta_pairs(manifest.profile_path)
    if delta_summary.get("pair_count", 0) > 0:
        _, delta_new = update_backlog(backlog_path, "delta", evaluate_delta(delta_summary))
    run_new: list = []
    run_summary = summarize_run_records(manifest.profile_path)
    if run_summary.get("qualifying_run_count", 0) > 0:
        _, run_new = update_backlog(backlog_path, "process", evaluate_process(run_summary))
    _maybe_notify(manifest, sys_new + delta_new + run_new)
    print(json.dumps(record, indent=2))
    return int(record["returncode"])


def _summarize(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    summary = summarize_samples(manifest.profile_path)
    write_summary(manifest.summary_path, summary)
    print(json.dumps(summary, indent=2))
    return 0


def _host_summarize(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    summary = summarize_samples(manifest.host_profile_path)
    write_summary(manifest.host_summary_path, summary)
    print(json.dumps(summary, indent=2))
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .server import serve

    manifest = load_manifest(args.manifest)
    serve(
        manifest.repo_root,
        host=args.host,
        host_summary_path=manifest.host_summary_path,
        port=args.port,
        allow_remote=args.allow_remote,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared repo and resource profiling helpers")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Collect one repo snapshot")
    snapshot_parser.add_argument("--manifest", required=True)
    snapshot_parser.add_argument("--label", default=None)
    snapshot_parser.add_argument("--phase", default="snapshot")
    snapshot_parser.set_defaults(func=_snapshot)

    host_snapshot_parser = subparsers.add_parser(
        "host-snapshot",
        help="Collect one canonical host snapshot",
    )
    host_snapshot_parser.add_argument("--manifest", required=True)
    host_snapshot_parser.add_argument("--label", default=None)
    host_snapshot_parser.add_argument("--phase", default="snapshot")
    host_snapshot_parser.set_defaults(func=_host_snapshot)

    agent_utilization_parser = subparsers.add_parser(
        "agent-utilization",
        help="Collect local AI provider utilization from CLI caches",
    )
    agent_utilization_parser.add_argument("--manifest", required=True)
    agent_utilization_parser.set_defaults(func=_agent_utilization)

    run_parser = subparsers.add_parser("run", help="Profile a command with pre/post samples")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--name", default=None)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=_run)

    summarize_parser = subparsers.add_parser("summarize", help="Print the current JSON summary")
    summarize_parser.add_argument("--manifest", required=True)
    summarize_parser.set_defaults(func=_summarize)

    host_summarize_parser = subparsers.add_parser(
        "host-summarize",
        help="Print the canonical host JSON summary",
    )
    host_summarize_parser.add_argument("--manifest", required=True)
    host_summarize_parser.set_defaults(func=_host_summarize)

    serve_parser = subparsers.add_parser("serve", help="Start the portfolio dashboard web server")
    serve_parser.add_argument("--manifest", required=True, help="Path to tachometer profile.toml")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5100)
    serve_parser.add_argument("--allow-remote", action="store_true")
    serve_parser.set_defaults(func=_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
