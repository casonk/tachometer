# tachometer

Shared repo and resource profiling helpers for the portfolio.

`tachometer` extracts the reusable profiling contract that first lived in
`doseido`: resource snapshots, persisted sample history, command profiling, and
portable JSON summaries. Downstream repos keep their own workload logic.
`tachometer` owns the common profiling model, CLI, and repo-local manifest
convention.

## Scope

`tachometer` currently provides:

- host resource snapshots: CPU, load average, memory, disk, and optional GPU
- canonical host profile and summary artifacts for the dashboard banner
- local AI-provider utilization snapshots for Claude, Codex, and Copilot when
  their local CLI caches expose usage
- optional Fedora-specific sidecar signals exported by `fedora-debugg`
- repo snapshots: repo size, file and directory counts, and git tracked/dirty
  or untracked counts when git is available
- profiled command runs with pre and post samples plus runtime metadata
- manifest-driven profile and summary paths so every repo can use the same
  local output convention

`tachometer` does not yet try to own:

- repo-specific stoplight policies or adaptive controller logic
- portfolio-wide dashboards or central metric shipping
- long-running daemon collection

That split is intentional. `doseido` keeps its controller and system monitor;
`tachometer` owns the lower-level profiling primitive those features depend on.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

## CLI

Capture a repo snapshot from a tracked manifest:

```bash
tachometer snapshot --manifest examples/doseido/repo-profile.toml
```

Capture the canonical host snapshot used by the portfolio dashboard banner:

```bash
tachometer host-snapshot --manifest config/tachometer/profile.toml
```

Profile a command and append pre/post samples plus a run record:

```bash
tachometer run --manifest examples/doseido/repo-profile.toml -- python3 -m pytest -q
```

Print the current JSON summary:

```bash
tachometer summarize --manifest examples/doseido/repo-profile.toml
```

Print the current canonical host summary:

```bash
tachometer host-summarize --manifest config/tachometer/profile.toml
```

Capture the local AI-provider utilization snapshot used by the dashboard:

```bash
tachometer agent-utilization --manifest config/tachometer/profile.toml
```

Serve the dashboard on loopback only:

```bash
tachometer serve --manifest config/tachometer/profile.toml --host 127.0.0.1 --port 5100
```

Non-loopback binds are blocked unless you pass `--allow-remote` explicitly.

If `fedora-debugg` has exported `artifacts/latest/tachometer-signals.json`,
the dashboard also renders a separate Fedora Debug strip with snapshot age plus
bucketed Collection, Display, Coredumps, GPU, Storage, Packages, Python, Node,
and Go signals.

If `.tachometer/agent-utilization.json` exists, the dashboard also renders an
AI Utilization strip sourced from local CLI state:

- Claude: `~/.claude/stats-cache.json` plus `claude auth status --json`
- Codex: the latest `.codex` session `token_count` event with rate-limit data
- Copilot: the latest `.copilot/session-state/*/events.jsonl` shutdown record

Copilot usage only appears after at least one session has started, and the
current session totals land when that session shuts down.

## Manifest Shape

```toml
[repo]
name = "doseido"
category = "health-repos"
kind = "python"

[paths]
disk_path = "."
profile_path = ".tachometer/profile.json"
summary_path = ".tachometer/summary.json"
host_profile_path = ".tachometer/host-profile.json"
host_summary_path = ".tachometer/host-summary.json"

[defaults]
label = "repo-snapshot"
```

Manifest rules:

- manifests live at `config/tachometer/profile.toml` in downstream repos
- the repo root is inferred from the manifest location
- `.tachometer/` is the standard local-only output directory
- `disk_path` is resolved relative to the repo root
- `host_profile_path` and `host_summary_path` default to canonical dashboard artifacts
  and are primarily used by the `tachometer` repo itself

## Portfolio Rollout

The shared utility repo is paired with a lightweight downstream convention:

- `config/tachometer/profile.toml`
- `scripts/run_tachometer_profile.sh`
- `.gitignore` entry for `.tachometer/`

That gives every repo one stable local profiling entrypoint while keeping the
actual profiler implementation centralized here.

## Development

```bash
ruff check .
ruff format --check .
black --check --diff .
pytest -q
```

## Contributing

See `CONTRIBUTING.md`.

## License

MIT. See `LICENSE`.
