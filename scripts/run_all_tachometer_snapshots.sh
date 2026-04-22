#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tachometer_root="$(cd "$script_dir/.." && pwd)"
portfolio_root="$(cd "$tachometer_root/../.." && pwd)"
tachometer_src="$tachometer_root/src"
downstream_config="$tachometer_root/config/downstream-repos.toml"

# Python emits one line per repo: <abs_path>\t<run_command>\t<no_run_reason>
# Empty run_command / no_run_reason fields are written as empty strings.
_repo_lines() {
  PYTHONPATH="$tachometer_src" python3 - "$portfolio_root" "$downstream_config" <<'PYEOF'
import sys
import pathlib
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

portfolio_root = pathlib.Path(sys.argv[1])
config_path = pathlib.Path(sys.argv[2])
data = tomllib.loads(config_path.read_text(encoding="utf-8"))
for repo in data.get("repos", []):
    path = repo.get("path", "").lstrip("./")
    run_command = repo.get("run_command", "")
    no_run_reason = repo.get("no_run_reason", "")
    if path:
        print(f"{portfolio_root / path}\t{run_command}\t{no_run_reason}")
PYEOF
  # tachometer self-profile
  printf "%s\t%s\t\n" "$tachometer_root" "python3 -m pytest tests/ -q"
}

failed=0
manifest_path="$tachometer_root/config/tachometer/profile.toml"

printf "==> host: snapshot\n"
if ! PYTHONPATH="$tachometer_src" python3 -m tachometer.cli host-snapshot --manifest "$manifest_path"; then
  printf "FAILED host snapshot: %s\n" "$tachometer_root" >&2
  ((failed++)) || true
fi

printf "==> host: agent utilization\n"
if ! PYTHONPATH="$tachometer_src" python3 -m tachometer.cli agent-utilization --manifest "$manifest_path"; then
  printf "FAILED agent utilization snapshot: %s\n" "$tachometer_root" >&2
  ((failed++)) || true
fi

while IFS=$'\t' read -r repo_dir run_cmd no_run_reason; do
  runner="$repo_dir/scripts/run_tachometer_profile.sh"
  name="$(basename "$repo_dir")"

  if [ ! -f "$runner" ]; then
    printf "SKIP %s (no runner script)\n" "$name"
    continue
  fi

  printf "==> %s: snapshot\n" "$name"
  if ! bash "$runner" snapshot; then
    printf "FAILED snapshot: %s\n" "$repo_dir" >&2
    ((failed++)) || true
    continue
  fi

  if [ -n "$run_cmd" ]; then
    printf "==> %s: run (%s)\n" "$name" "$run_cmd"
    # run failures are non-fatal — we still want psutil data even if tests fail
    bash "$runner" run -- bash -c "$run_cmd" || \
      printf "  run exited non-zero for %s (recorded in profile)\n" "$name" >&2
  elif [ -n "$no_run_reason" ]; then
    printf "    skip run: %s\n" "$no_run_reason"
  fi

done < <(_repo_lines)

if [ "$failed" -gt 0 ]; then
  printf "\n%d snapshot(s) failed.\n" "$failed" >&2
  exit 1
fi
printf "\nAll snapshots complete.\n"
