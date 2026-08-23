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
