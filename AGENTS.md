# AGENTS.md — tachometer

## Purpose

`tachometer` is the shared profiling repo for the portfolio. It owns the
portable profiling abstraction used to collect:

- host resource snapshots
- repo-size and git-state snapshots
- profiled command runs with pre and post measurements
- persisted JSON profile histories and summaries

Keep the repository focused on reusable profiling mechanics. Repo-specific
backoff logic, dashboards, and workload policies stay in the downstream repo.

## Repository Layout

- `src/tachometer/model.py`: manifest and snapshot dataclasses
- `src/tachometer/manifest.py`: TOML manifest loading and validation
- `src/tachometer/profile.py`: snapshot collection, run profiling, and JSON
  persistence helpers
- `src/tachometer/cli.py`: CLI entrypoint for snapshot, run, and summarize
- `examples/`: current portfolio examples
- `config/downstream-repos.toml`: repos that use the shared profiling
  convention
- `tests/`: unit coverage for manifest loading, profiling, and CLI flows

## Setup And Commands

Recommended repo-root workflow:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
pytest -q
```

Useful commands:

```bash
tachometer snapshot --manifest examples/doseido/repo-profile.toml
tachometer run --manifest examples/doseido/repo-profile.toml -- python3 -m pytest -q
tachometer summarize --manifest examples/doseido/repo-profile.toml
```

## Operating Rules

1. Keep profiling output deterministic and JSON-serializable.
2. Prefer placeholder paths and synthetic examples over machine-specific local
   data.
3. Downstream repos should keep profiler outputs local-only under `.tachometer/`
   unless a tracked fixture is explicitly needed for tests.
4. When the downstream convention changes, update the example manifest and
   downstream inventory in the same change.
5. Run repo-appropriate validation after schema, persistence, or CLI changes.

## Portfolio References

Portfolio-wide standards live in `./util-repos/traction-control` from the
portfolio root.

Shared implementation repos available portfolio-wide are:

- `./util-repos/archility` for architecture toolchain bootstrap/rendering and
  architecture-documentation drift checks
- `./util-repos/auto-pass` for KeePassXC-backed password management
- `./util-repos/clockwork` for shared cron and `systemd` scheduling
- `./util-repos/nordility` for VPN switching
- `./util-repos/shock-relay` for external messaging
- `./util-repos/short-circuit` for WireGuard VPN setup and configuration
- `./util-repos/snowbridge` for SMB-based file sharing
- `./util-repos/dyno-lab` for shared test fixtures and helpers
- `./util-repos/tachometer` for shared profiling and resource snapshots

When another repo needs local profiling or resource-utilization tracking,
prefer integrating with `tachometer` instead of growing another repo-local
profiler implementation.

## Local CI Verification

Run before every push:

```bash
pre-commit run --all-files
pytest -q
```

Do not push changes that have not passed all checks locally.

## Agent Memory

Use `./LESSONSLEARNED.md` as the tracked durable lessons file for this repo.
Use `./CHATHISTORY.md` as the local-only handoff file for this repo.

- `LESSONSLEARNED.md` is tracked and should capture reusable lessons only.
- `CHATHISTORY.md` is gitignored and must not be committed.
- Read `LESSONSLEARNED.md` and `CHATHISTORY.md` after `AGENTS.md` when resuming
  work.
