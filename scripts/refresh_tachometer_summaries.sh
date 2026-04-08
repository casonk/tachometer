#!/usr/bin/env bash
# Re-run `tachometer summarize` for every portfolio repo without taking new
# snapshots.  Use this after updating summarize_samples() to backfill any new
# fields into existing summary.json files from already-collected profile.json data.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tachometer_root="$(cd "$script_dir/.." && pwd)"
portfolio_root="$(cd "$tachometer_root/../.." && pwd)"
tachometer_src="$tachometer_root/src"
downstream_config="$tachometer_root/config/downstream-repos.toml"

mapfile -t repo_dirs < <(
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
    if path:
        print(portfolio_root / path)
PYEOF
)

repo_dirs+=("$tachometer_root")

run_tachometer() {
  if command -v tachometer >/dev/null 2>&1; then
    tachometer "$@"
    return 0
  fi
  PYTHONPATH="$tachometer_src${PYTHONPATH:+:$PYTHONPATH}" python3 -m tachometer "$@"
}

failed=0
for repo_dir in "${repo_dirs[@]}"; do
  manifest="$repo_dir/config/tachometer/profile.toml"
  if [ ! -f "$manifest" ]; then
    printf "SKIP %s (no manifest)\n" "$(basename "$repo_dir")"
    continue
  fi
  printf "==> %s\n" "$(basename "$repo_dir")"
  if ! run_tachometer summarize --manifest "$manifest" > /dev/null; then
    printf "FAILED: %s\n" "$repo_dir" >&2
    ((failed++)) || true
  fi
done

if [ "$failed" -gt 0 ]; then
  printf "\n%d summarize(s) failed.\n" "$failed" >&2
  exit 1
fi
printf "\nAll summaries refreshed.\n"
