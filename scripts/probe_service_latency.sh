#!/usr/bin/env bash
# Probe every configured service's connect and reply latency and append the result to
# .tachometer/service-latency.jsonl; alerts go through the manifest's [notify] settings.
# Cheap enough to run every few minutes; settings live in config/tachometer/service-latency.local.toml.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/config/tachometer/profile.toml"

if command -v tachometer >/dev/null 2>&1; then
  exec tachometer service-latency probe --manifest "$manifest" "$@"
fi
PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tachometer service-latency probe --manifest "$manifest" "$@"
