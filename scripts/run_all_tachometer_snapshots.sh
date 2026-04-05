#!/usr/bin/env bash
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

failed=0
for repo_dir in "${repo_dirs[@]}"; do
  runner="$repo_dir/scripts/run_tachometer_profile.sh"
  if [ ! -f "$runner" ]; then
    printf "SKIP %s (no runner script)\n" "$(basename "$repo_dir")"
    continue
  fi
  printf "==> %s\n" "$(basename "$repo_dir")"
  if ! bash "$runner" snapshot; then
    printf "FAILED: %s\n" "$repo_dir" >&2
    ((failed++)) || true
  fi
done

if [ "$failed" -gt 0 ]; then
  printf "\n%d snapshot(s) failed.\n" "$failed" >&2
  exit 1
fi
printf "\nAll snapshots complete.\n"
