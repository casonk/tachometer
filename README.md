# tachometer

Shared repo and resource profiling helpers for the portfolio.

`tachometer` extracts the reusable profiling contract that first lived in
`private-repository`: resource snapshots, persisted sample history, command profiling, and
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

That split is intentional. `private-repository` keeps its controller and system monitor;
`tachometer` owns the lower-level profiling primitive those features depend on.

## Platform support

The package installs and the CLI runs on Linux, macOS and Windows, verified by
CI on every push. **How much it can actually measure differs sharply**, because
most signals come from interfaces only Linux provides.

| Signal source | Linux | macOS | Windows |
| --- | :---: | :---: | :---: |
| Repo metrics (size, file counts, git) | yes | yes | yes |
| Disk usage | yes | yes | yes |
| Child CPU time, peak RSS, page faults (`resource`) | yes | yes | **no** |
| Per-process CPU, memory, I/O (`psutil`, optional) | yes | yes | yes |
| Load average, memory detail, uptime, process count (`/proc`) | yes | **no** | **no** |
| CPU temperature, fan RPM (`/sys/class`) | yes | **no** | **no** |
| Energy in joules (Intel RAPL) | yes | **no** | **no** |
| GPU (`nvidia-smi`) | if present | if present | if present |

### What this means in practice

**Linux** is the reference platform. Everything above works.

**macOS** loses the `/proc` and `/sys` signals — no load average detail, memory
breakdown, uptime, process count, temperature, fan or energy. Child-process CPU
and memory still work, because `resource` is a Unix module.

**Windows** additionally loses `resource`, which does not exist there at all.
Without `psutil` installed, a profiled command reports `None` for CPU percent,
peak RSS and fault counts.

`psutil` is not a declared dependency, so on Windows it is worth installing
explicitly for anything beyond timing:

```bash
pip install psutil
```

Fields that cannot be measured are reported as `None` rather than `0`, so a
missing signal is distinguishable from a genuine zero. Widening native Windows
coverage is tracked in [`BACKLOG.md`](BACKLOG.md).

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

## CLI

Capture a repo snapshot from a tracked manifest:

```bash
tachometer snapshot --manifest examples/private-repository/repo-profile.toml
```

Capture the canonical host snapshot used by the portfolio dashboard banner:

```bash
tachometer host-snapshot --manifest config/tachometer/profile.toml
```

Profile a command and append pre/post samples plus a run record:

```bash
tachometer run --manifest examples/private-repository/repo-profile.toml -- python3 -m pytest -q
```

Print the current JSON summary:

```bash
tachometer summarize --manifest examples/private-repository/repo-profile.toml
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

## Disk Pressure Response

When disk utilization crosses the red threshold, `tachometer` writes an open
`system.disk` or `host.disk` item to the local `.tachometer/backlog.json` /
`.tachometer/host-backlog.json` file. The shared remediation path lives in
`./util-repos/traction-control`: its tachometer disk-pressure agentic job scans
those backlog and summary files, selects clean pressure candidates, and launches
an agent to add reversible repo-local archive automation.

The standard fix pattern is intentionally conservative: compress/decompress
local-only caches, generated artefacts, temporary downloads, and debug snapshots
with an audit trail; never delete source data or raw private inputs as the
default pressure response.

## Manifest Shape

```toml
[repo]
name = "private-repository"
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
