# Profiling Backlog

All originally-planned items are now implemented.

## Implemented

### System view
- ✅ **Load average** — `avg_loadavg_1m` normalised by `cpu_count`, subline in CPU cell; stoplight green <70%, yellow <90%
- ✅ **Swap usage** — `/proc/meminfo` SwapTotal/SwapFree, swap% subline in Memory cell; green <10%, yellow <40%
- ✅ **GPU memory used** — `avg_gpu_mem_used_mb`, VRAM% subline in GPU cell; green <70%, yellow <90%
- ✅ **Git commit count** — `git rev-list --count HEAD`, shown in Repo cell
- ✅ **Dependency count** — scans `requirements*.txt` + `pyproject.toml`, shown in Repo cell
- ✅ **Build artefact size** — scans `dist/`, `build/`, `*.egg-info/`, shown in Repo Size cell; green <100 MB, yellow <1 GB

### Delta view
- ✅ **Disk I/O** — `psutil.disk_io_counters()` delta, combined ΔDisk I/O cell (↓read / ↑write)
- ✅ **Network I/O** — `psutil.net_io_counters()` delta, ΔNetwork cell (↓recv / ↑sent)

### Process view
- ✅ **Runtime duration trend** — `avg_runtime_seconds` with latest/max subline; green <60s, yellow <300s
- ✅ **Thread count peak** — sampled in `_monitor_process`, shown in Extras cell; green <100, yellow <500
- ✅ **Page faults** — rusage `ru_majflt` delta, major faults in Extras cell; green <500, yellow <5000
- ✅ **Context switches** — cumulative `num_ctx_switches()` delta from first→last psutil sample, involuntary count in Extras cell; green <10k, yellow <100k
- ✅ **Energy / power** — Intel RAPL `/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj` delta, shown as joules in Extras cell (informational, no stoplight)

## Future ideas

- [ ] [manual:2026-10-15] **Ask GitHub Support to purge pre-rewrite commits** — this
  repository's published history was rewritten (most recently on 2026-10-15,
  to remove per-commit AI attribution trailers). GitHub keeps serving the old
  commits to anyone with a hash, and through the read-only `refs/pull/*` refs
  of earlier pull requests, until Support garbage-collects them. The request
  is filed once for the whole portfolio from traction-control's backlog; mark
  this done when an old hash returns 404 here. Never push an old clone's
  history back.

- [ ] [manual:2026-10-18] **PCIe link rev limiters.** Add a row per NVMe drive and GPU
  to the `/thermal` Rev limiters table with the current link speed and width
  against the device's and port's maximum (`/sys/bus/pci/devices/*/current_link_speed`,
  `current_link_width`, `max_link_*`, no root needed), amber when a link trains
  below what both ends support. On the workstation both NVMe drives run PCIe 3.0
  x2 although they are x4 devices; this was found only by hand while tuning
  read-ahead (see fedora-debugg's backlog for the hardware review).
- [ ] [manual:2026-10-18] **Firmware in the host view.** Show the board and BIOS
  version and date (`/sys/class/dmi/id/{board_name,bios_version,bios_date}`,
  readable without root) on `/thermal` or the host banner, so an outdated BIOS
  (currently 3006 from 2021-10-12) stays visible.

- [ ] [manual:2026-10-18] **Benchmark suite (longer, repeatable).** The read-ahead
  tuning on the workstation showed both how much a measurement can reveal (LUKS on
  NVMe went 485 -> 1404 MB/s) and how noisy short runs are (the same 2 MiB setting
  measured 1404 and 1278 MB/s in two sessions). Add a `tachometer bench` that runs
  longer, repeated tests and keeps the results as history: cold sequential and
  random reads per volume (fio, or the posix_fadvise-based script used for the
  read-ahead sweep), including read amplification from `/proc/diskstats`; memory
  bandwidth; CPU and GPU sustained load with temperature, power and clock traces
  (a soak test that also shows when a rev limiter engages); medians with spread,
  not single numbers. Run it on demand before and after any hardware, BIOS or
  tuning change, and on a slow schedule when the machine is idle, and chart the
  history next to `/thermal`.

### CPU temperature on Apple Silicon

The root-run powermetrics collector gives Intel Macs CPU and GPU die
temperatures and every Mac a thermal-pressure light, but Apple Silicon
`powermetrics` reports no temperatures. Its die sensors are reachable only
through private IOKit HID interfaces; add a reader only if one proves stable
across macOS releases, and keep it in the root collector rather than the
user-run sampler.

### Detailed monitoring on Windows

The package installs and runs on Windows, but measures very little there. The
`resource` module does not exist, and every `/proc` and `/sys` signal is
Linux-only, so without `psutil` a profiled command reports `None` for CPU
percent, peak RSS and fault counts. See the platform table in `README.md`.

Worth supporting properly if anything in the portfolio is ever profiled on
Windows. Roughly in order of value per unit of effort:

- **Make `psutil` the primary collector rather than the fallback** — it already
  supports Windows and supplies per-process CPU, memory, I/O and context
  switches. Today rusage is preferred and psutil fills gaps; inverting that
  would make Windows a first-class path without new dependencies on Linux.
  Largest win, and mostly a reordering of existing code.
- **Declare `psutil` as an optional extra** — `pip install tachometer[metrics]`,
  so the richer path is discoverable instead of relying on the user happening
  to have it. Currently it is an undeclared soft dependency.
- **Windows peak working set** — `GetProcessMemoryInfo` via `ctypes`, or
  `psutil.Process.memory_info().peak_wset`, as the analogue of `ru_maxrss`.
- **Windows CPU time** — `GetProcessTimes` for kernel/user split, the analogue
  of `ru_utime`/`ru_stime`.
- **Windows counterparts for the host snapshot** — load average has no direct
  equivalent; the closest is the processor queue length from performance
  counters. Uptime, process count and memory detail are all available through
  `psutil` without `/proc`.
- **Energy** — no RAPL equivalent is exposed on Windows. Intel Power Gadget is
  discontinued, so this may simply stay Linux-only, and saying so explicitly is
  a better outcome than leaving the field silently empty.

Non-goal for now: temperature and fan RPM. Both need vendor-specific WMI
providers or kernel drivers, and the payoff does not justify that surface.

### Other

- **Per-process network I/O** — `/proc/<pid>/net/dev` delta for the process tree; more precise than system-wide counters
- **Dependency vulnerability scan** — run `pip-audit` or `safety` in the background and surface CVE counts
- **Test coverage trend** — parse `.coverage` or `coverage.xml` and track line/branch coverage over time
- **Cold-start time** — measure time-to-first-output for CLI tools (import overhead)
