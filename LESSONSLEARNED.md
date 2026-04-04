# LESSONSLEARNED.md

Tracked durable lessons for `tachometer`.
Unlike `CHATHISTORY.md`, this file should keep only reusable lessons that
should change how future sessions work in this repo.

## How To Use

- Read this file after `AGENTS.md` and before `CHATHISTORY.md` when resuming
  work.
- Add lessons that generalize beyond a single session.
- Keep entries concise and action-oriented.
- Do not use this file for transient status updates or full session logs.

## Lessons

- Keep repo-local manifests and examples free of machine-specific absolute
  paths; tracked profiling conventions should travel cleanly between portfolio
  repos and fresh clones.
- Preserve the `doseido` profiler field names in shared summaries unless a
  downstream migration explicitly coordinates a schema change.
- Keep profiling outputs local-only under `.tachometer/` so tracked repos do
  not accumulate host-specific resource histories by default.
