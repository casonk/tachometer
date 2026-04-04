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

Profile a command and append pre/post samples plus a run record:

```bash
tachometer run --manifest examples/doseido/repo-profile.toml -- python3 -m pytest -q
```

Print the current JSON summary:

```bash
tachometer summarize --manifest examples/doseido/repo-profile.toml
```

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

[defaults]
label = "repo-snapshot"
```

Manifest rules:

- manifests live at `config/tachometer/profile.toml` in downstream repos
- the repo root is inferred from the manifest location
- `.tachometer/` is the standard local-only output directory
- `disk_path` is resolved relative to the repo root

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
