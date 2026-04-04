# Contributor Architecture Blueprint

## Overview

`tachometer` is the shared profiling utility for the portfolio. It turns a
small repo-local manifest into three portable actions:

1. collect a host and repo snapshot
2. profile a command with pre and post samples
3. summarize persisted sample history into stable JSON output

Downstream repos keep their own workload logic and call `tachometer` through
their local `config/tachometer/profile.toml` plus a thin shell wrapper.

## Main Flow

1. A downstream repo invokes `tachometer snapshot`, `tachometer run`, or
   `tachometer summarize`.
2. `tachometer.manifest` loads the tracked repo-local manifest and resolves the
   repo root plus local output paths.
3. `tachometer.profile` captures resource metrics:
   - CPU and load average from `/proc` or `os.getloadavg()`
   - memory data from `/proc/meminfo`
   - disk usage from `shutil.disk_usage`
   - optional GPU data from `nvidia-smi`
   - repo size and git-state data from the repo root
4. Samples and run records are appended to the local `.tachometer/profile.json`
   document.
5. `tachometer.profile.summarize_samples()` emits the stable summary written to
   `.tachometer/summary.json` or printed to stdout.

## Module Responsibilities

- `tachometer.model`
  - manifest dataclasses
  - snapshot dataclass used by both CLI and downstream repos
- `tachometer.manifest`
  - TOML parsing
  - repo-root inference from the tracked manifest location
  - path resolution for local output files
- `tachometer.profile`
  - system metric collection
  - repo metric collection
  - JSON persistence and summary generation
  - profiled command execution
- `tachometer.cli`
  - user-facing command surface for snapshot, run, and summarize

## Runtime Files

- `config/downstream-repos.toml`
  - tracked inventory of downstream repos using the shared profiling
    convention
- `examples/`
  - tracked reference manifests
- `.tachometer/profile.json`
  - local-only sample and run history in downstream repos
- `.tachometer/summary.json`
  - local-only summarized view of the current profile history

## Change Rules

- Keep the JSON field names stable unless downstream migrations are updated in
  the same change.
- Do not add repo-specific stoplight logic here unless it clearly generalizes.
- Keep local output paths under `.tachometer/` and out of git by default.
