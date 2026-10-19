#!/usr/bin/env bash
# Nightly benchmark entry point: runs the most overdue tier (weekly short,
# monthly mid, quarterly full) inside the 02:00-05:00 quiet window, or nothing.
# Pass --tier short|mid|full instead of the default --auto to run one now.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/config/tachometer/profile.toml"
args=("$@")
[ ${#args[@]} -eq 0 ] && args=(--auto)

if command -v tachometer >/dev/null 2>&1; then
  exec tachometer bench --manifest "$manifest" "${args[@]}"
fi
PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tachometer bench --manifest "$manifest" "${args[@]}"
