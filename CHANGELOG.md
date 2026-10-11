# Changelog

## Unreleased

- The host sampler now reads SMART history when it is available. If
  `/var/lib/tire-rotation/smart/*.jsonl` exists (written by tire-rotation's
  root timer; override with `TACHOMETER_SMART_DIR`), each sample includes the
  latest fresh SMART temperature per drive alongside the kernel's readings, and
  `/thermal` shows a Drive health chip: red when a drive fails its SMART
  health check, yellow when a fault counter rose since the previous snapshot.
  Snapshots older than three hours are ignored. The sampler still needs no
  root; it only reads.
- Thermal history retention is now tiered. Five-minute samples are kept for 30
  days (was 14); older ones are folded into one record per hour (minimum, mean,
  maximum) in `.tachometer/host-samples-hourly.jsonl` and kept for 90 days.
  `/thermal` gains 30d and 90d ranges drawn from hourly points: peak
  temperature and fan speed, mean load. `sample-host` accepts `--keep-days` and
  `--aggregate-days`.
- Added a host thermal history. `tachometer sample-host` (wrapper
  `scripts/sample_host.sh`, timer template
  `config/clockwork/host-sampler.toml.template`) appends CPU, GPU, board and
  per-drive temperatures, fan speed and load to
  `.tachometer/host-samples.jsonl` every five minutes and keeps 14 days. The
  dashboard serves it at `/thermal` (and `/api/thermal`) as a time series with a
  crosshair readout, 6h/24h/7d ranges, a table view, and a stoplight per
  reading. Everything is read without root; SATA drive temperatures need the
  kernel's `drivetemp` module.
- Fixed the CPU temperature reading. `_read_cpu_temp` returned the first
  thermal zone with a value, which on many desktops is the ACPI board sensor
  (`acpitz`) and sits near room temperature while the processor is at its
  limit. It now reads only a zone or hwmon chip that names the CPU package
  (`x86_pkg_temp`, `coretemp`, `k10temp`, ...) and reports no value rather than
  an unrelated sensor.
- The host banner now colours CPU and GPU temperature (yellow from 80 C / 78 C,
  red from 90 C / 85 C) instead of always showing them as neutral.
- Documented what `tachometer` can actually measure per platform. The README
  now carries a signal-by-signal table: Linux is the reference platform, macOS
  loses every `/proc` and `/sys` signal, and Windows additionally loses
  `resource`, leaving CPU percent, peak RSS and fault counts as `None` unless
  `psutil` is installed. Unmeasurable fields report `None` rather than `0`, so
  a missing signal stays distinguishable from a genuine zero.
- Added detailed Windows monitoring to `BACKLOG.md`, ordered by value per unit
  of effort. The largest win is inverting the collector precedence so `psutil`
  leads and rusage fills gaps, which would make Windows first-class without
  adding a dependency on Linux.
- Fixed the package being unimportable on Windows. `profile.py` imported the
  Unix-only `resource` module at module scope, so `import tachometer` raised
  `ModuleNotFoundError: No module named 'resource'` — despite the package
  advertising `Operating System :: OS Independent`. The import is now guarded
  the same way `psutil` already was, and the rusage-derived metrics report
  `None` where the platform cannot supply them rather than a fabricated zero.
- Added the shared `install-check` workflow, which installs the package on
  Linux, macOS and Windows and runs the console script. It is what found the
  import failure above.
- Initial `clockwork` scaffold for declarative cron and systemd rendering.
- Documented tachometer disk red lights as the trigger contract for the shared traction-control disk-pressure remediation agent.
