#!/usr/bin/env bash
# Append one host temperature and load sample to .tachometer/host-samples.jsonl.
# Cheap enough to run every few minutes; the dashboard's /thermal page charts it.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/config/tachometer/profile.toml"

if command -v tachometer >/dev/null 2>&1; then
  exec tachometer sample-host --manifest "$manifest" "$@"
fi
PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tachometer sample-host --manifest "$manifest" "$@"
