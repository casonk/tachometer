#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/config/tachometer/profile.toml"
port="${TACHOMETER_PORT:-5100}"

find_tachometer_src() {
  local dir="$repo_root"
  while [ "$dir" != "/" ]; do
    if [ -d "$dir/util-repos/tachometer/src" ]; then
      printf "%s\n" "$dir/util-repos/tachometer/src"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

run_tachometer() {
  if command -v tachometer >/dev/null 2>&1; then
    tachometer "$@"
    return 0
  fi
  local tachometer_src
  if ! tachometer_src="$(find_tachometer_src)"; then
    printf "tachometer is not installed and util-repos/tachometer/src was not found from %s\n" "$repo_root" >&2
    return 1
  fi
  PYTHONPATH="$tachometer_src${PYTHONPATH:+:$PYTHONPATH}" python3 -m tachometer "$@"
}

run_tachometer serve --manifest "$manifest" --port "$port"
