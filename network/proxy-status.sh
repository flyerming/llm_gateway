#!/bin/sh
# Show mihomo proxy status.

set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
MIHOMO_BIN="${SCRIPT_DIR}/mihomo-linux-amd64"
MIHOMO_HOME="${SCRIPT_DIR}/mihomo"
PID_FILE="${TMPDIR:-/tmp}/mihomo.pid"
LOG_FILE="${TMPDIR:-/tmp}/mihomo.log"
CONTROLLER_URL="${MIHOMO_CONTROLLER_URL:-http://127.0.0.1:9090}"
PROXY_URL="${MIHOMO_PROXY_URL:-http://127.0.0.1:7890}"
SELECTOR_NAME="${MIHOMO_SELECTOR:-🚀 节点选择}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODES_PY="${SCRIPT_DIR}/proxy-nodes.py"
PROXIES_JSON="${TMPDIR:-/tmp}/mihomo-status.$$.json"

# Node names contain emoji and Chinese characters, so the output stream must be
# UTF-8 even when the server shell runs under the C/POSIX locale.
PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export PYTHONIOENCODING

trap 'rm -f "${PROXIES_JSON}"' EXIT HUP INT TERM

echo "=== Paths ==="
echo "script dir: ${SCRIPT_DIR}"
echo "mihomo bin: ${MIHOMO_BIN}"
echo "mihomo dir: ${MIHOMO_HOME}"
echo "pid file:   ${PID_FILE}"
echo "log file:   ${LOG_FILE}"

echo ""
echo "=== Process ==="
if [ -f "${PID_FILE}" ]; then
    echo "pid: $(cat "${PID_FILE}")"
fi
ps aux | grep mihomo | grep -v grep || echo "not running"

echo ""
echo "=== Shell Proxy Env ==="
env | grep -iE '^(http_proxy|https_proxy|all_proxy|no_proxy)=' || echo "no proxy env set in this shell"
echo "enable mihomo for normal commands with:"
echo "  . \"${SCRIPT_DIR}/proxy-env.sh\""

echo ""
echo "=== Current Node ==="
if curl -s --max-time 8 "${CONTROLLER_URL}/proxies" -o "${PROXIES_JSON}"; then
    "${PYTHON_BIN}" "${NODES_PY}" now "${SELECTOR_NAME}" < "${PROXIES_JSON}" || true
    echo "list all nodes with:"
    echo "  sh \"${SCRIPT_DIR}/proxy-switch.sh\" -a"
else
    echo "API not responding, maybe mihomo is not running"
fi

echo ""
echo "=== Proxy Test ==="
curl -s --max-time 8 -x "${PROXY_URL}" https://httpbin.org/ip || echo "proxy test failed"

echo ""
echo "=== Log Tail ==="
tail -n 5 "${LOG_FILE}" 2>/dev/null || echo "no log file"
