#!/bin/sh
# Stop mihomo proxy started from this script directory.

set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
MIHOMO_BIN="${SCRIPT_DIR}/mihomo-linux-amd64"
PID_FILE="${TMPDIR:-/tmp}/mihomo.pid"

if pkill -f "${MIHOMO_BIN}"; then
    rm -f "${PID_FILE}"
    echo "mihomo stopped"
else
    echo "mihomo is not running"
fi

echo "to remove proxy variables from this shell, run:"
echo "  . \"${SCRIPT_DIR}/proxy-unset.sh\""
