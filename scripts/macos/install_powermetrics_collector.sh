#!/bin/bash
# Install (or remove) the root-run macOS powermetrics collector.
#
#   sudo bash scripts/macos/install_powermetrics_collector.sh            # install or update
#   sudo bash scripts/macos/install_powermetrics_collector.sh --uninstall
#
# The LaunchDaemon runs a root-owned copy of powermetrics_collector.py from
# /usr/local/libexec/tachometer, never the checkout: a root job must not
# execute a file the user can edit. Readings go to
# /var/db/tachometer/powermetrics.jsonl (root-owned, world-readable). Point the
# user-run host sampler at that file with TACHOMETER_MACOS_POWERMETRICS.

set -euo pipefail

LABEL="local.tachometer.powermetrics"
LIBEXEC="/usr/local/libexec/tachometer"
COLLECTOR="${LIBEXEC}/powermetrics_collector.py"
DATA_DIR="/var/db/tachometer"
OUTPUT="${DATA_DIR}/powermetrics.jsonl"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/powermetrics_collector.py"

if [[ "$(uname -s)" != Darwin ]]; then
  echo "macOS only" >&2; exit 2
fi
if [[ "$(id -u)" != 0 ]]; then
  echo "run with sudo" >&2; exit 2
fi

if [[ "${1:-}" == --uninstall ]]; then
  launchctl bootout "system/${LABEL}" 2>/dev/null || true
  rm -f "${PLIST}" "${COLLECTOR}"
  rmdir "${LIBEXEC}" 2>/dev/null || true
  echo "removed ${LABEL}; readings left in ${DATA_DIR}"
  exit 0
fi
[[ -z "${1:-}" ]] || { echo "unknown argument: $1" >&2; exit 2; }

install -d -o root -g wheel -m 755 "${LIBEXEC}" "${DATA_DIR}"
install -o root -g wheel -m 755 "${SOURCE}" "${COLLECTOR}"

cat > "${PLIST}.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>${LABEL}</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/python3</string>
		<string>${COLLECTOR}</string>
		<string>${OUTPUT}</string>
	</array>
	<key>StartInterval</key>
	<integer>300</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>${DATA_DIR}/powermetrics.err</string>
</dict>
</plist>
EOF
chown root:wheel "${PLIST}.tmp"
chmod 644 "${PLIST}.tmp"
plutil -lint "${PLIST}.tmp" >/dev/null
mv "${PLIST}.tmp" "${PLIST}"

launchctl bootout "system/${LABEL}" 2>/dev/null || true
launchctl bootstrap system "${PLIST}"
launchctl kickstart -k "system/${LABEL}"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -s "${OUTPUT}" ]] && break
  sleep 1
done
if [[ -s "${OUTPUT}" ]]; then
  echo "installed ${LABEL}; newest reading:"
  tail -n 1 "${OUTPUT}"
else
  echo "installed ${LABEL}, but no reading yet; see ${DATA_DIR}/powermetrics.err" >&2
fi
