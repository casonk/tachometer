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

- **Per-process network I/O** — `/proc/<pid>/net/dev` delta for the process tree; more precise than system-wide counters
- **Dependency vulnerability scan** — run `pip-audit` or `safety` in the background and surface CVE counts
- **Test coverage trend** — parse `.coverage` or `coverage.xml` and track line/branch coverage over time
- **Cold-start time** — measure time-to-first-output for CLI tools (import overhead)
