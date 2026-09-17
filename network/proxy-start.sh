#!/bin/sh
# Start mihomo proxy from this script directory.

set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
MIHOMO_BIN="${SCRIPT_DIR}/mihomo-linux-amd64"
MIHOMO_HOME="${SCRIPT_DIR}/mihomo"
PID_FILE="${TMPDIR:-/tmp}/mihomo.pid"
LOG_FILE="${TMPDIR:-/tmp}/mihomo.log"
PROXY_URL="${MIHOMO_PROXY_URL:-http://127.0.0.1:7890}"

if [ ! -f "${MIHOMO_BIN}" ]; then
    echo "missing mihomo binary: ${MIHOMO_BIN}" >&2
    exit 1
fi

if [ ! -d "${MIHOMO_HOME}" ]; then
    echo "missing mihomo config directory: ${MIHOMO_HOME}" >&2
    exit 1
fi

if [ ! -x "${MIHOMO_BIN}" ]; then
    chmod +x "${MIHOMO_BIN}" 2>/dev/null || {
        echo "mihomo binary is not executable: ${MIHOMO_BIN}" >&2
        exit 1
    }
fi

# 端口上已经有代理在应答，就不要再起第二个。容器化部署下最常见的情况是
# docker compose 里的 proxy 容器已经占了 7890，这时再起一个裸进程会互相抢端口。
if curl -s --max-time 3 -o /dev/null -x "${PROXY_URL}" https://icanhazip.com 2>/dev/null; then
    echo "已有代理在 ${PROXY_URL} 上应答，无需重复启动"
    echo ""
    echo "如果这是 docker compose 里的 proxy 容器，请用容器方式管理："
    echo "  docker compose ps"
    echo "  docker compose logs -f proxy"
    exit 0
fi

if pgrep -f "${MIHOMO_BIN}" > /dev/null; then
    echo "mihomo is already running"
    ps aux | grep mihomo | grep -v grep
    exit 0
fi

# Fail loudly here instead of letting mihomo exit in the background, where the
# only symptom would be a dead pid and a failing curl at the end of this script.
if ! "${MIHOMO_BIN}" -t -d "${MIHOMO_HOME}" > "${LOG_FILE}" 2>&1; then
    echo "config test failed, mihomo not started:" >&2
    tail -n 20 "${LOG_FILE}" >&2
    exit 1
fi

nohup "${MIHOMO_BIN}" -d "${MIHOMO_HOME}" > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"

sleep 2
echo "mihomo started, pid: $(cat "${PID_FILE}")"
echo "proxy url: ${PROXY_URL}"
echo "config dir: ${MIHOMO_HOME}"
echo "log file: ${LOG_FILE}"
echo ""
echo "to route normal curl/git/npm commands through mihomo in this shell, run:"
echo "  . \"${SCRIPT_DIR}/proxy-env.sh\""
echo ""
echo "testing proxy..."
curl -s --max-time 10 -x "${PROXY_URL}" https://icanhazip.com
