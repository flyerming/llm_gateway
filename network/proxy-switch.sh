#!/bin/sh
# Switch mihomo proxy node.

set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
CONTROLLER_URL="${MIHOMO_CONTROLLER_URL:-http://127.0.0.1:9090}"
PROXY_URL="${MIHOMO_PROXY_URL:-http://127.0.0.1:7890}"
SELECTOR_NAME="${MIHOMO_SELECTOR:-🚀 节点选择}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODES_PY="${SCRIPT_DIR}/proxy-nodes.py"
PROXIES_JSON="${TMPDIR:-/tmp}/mihomo-proxies.$$.json"
REQUEST_JSON="${TMPDIR:-/tmp}/mihomo-switch.$$.json"

# Node names contain emoji and Chinese characters. Without this the listing dies
# with UnicodeEncodeError on servers whose shell locale is C/POSIX.
PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export PYTHONIOENCODING

cleanup() {
    rm -f "${PROXIES_JSON}" "${REQUEST_JSON}"
}
trap cleanup EXIT HUP INT TERM

if ! command -v "${PYTHON_BIN}" > /dev/null 2>&1; then
    echo "missing python interpreter: ${PYTHON_BIN}" >&2
    echo "set PYTHON_BIN to a working python3" >&2
    exit 1
fi

if [ ! -f "${NODES_PY}" ]; then
    echo "missing helper: ${NODES_PY}" >&2
    exit 1
fi

print_usage() {
    echo "usage: $0 [-g \"group name\"] <node name | index>"
    echo "       $0 -l            list nodes of \"${SELECTOR_NAME}\""
    echo "       $0 -a            list every proxy group and every node"
    echo ""
    echo "env: MIHOMO_SELECTOR, MIHOMO_CONTROLLER_URL, MIHOMO_PROXY_URL"
    echo "     MIHOMO_ASCII=1 escapes emoji for terminals that cannot render it"
}

fetch_proxies() {
    if ! curl -s --max-time 8 "${CONTROLLER_URL}/proxies" -o "${PROXIES_JSON}"; then
        echo "cannot reach the mihomo controller at ${CONTROLLER_URL}" >&2
        echo "start mihomo first: sh \"${SCRIPT_DIR}/proxy-start.sh\"" >&2
        return 1
    fi
}

MODE=switch
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            MODE=help
            shift
            ;;
        -l|--list)
            MODE=list
            shift
            ;;
        -a|--all|--list-all)
            MODE=list-all
            shift
            ;;
        -g|--group)
            if [ $# -lt 2 ]; then
                echo "missing group name after $1" >&2
                exit 2
            fi
            SELECTOR_NAME="$2"
            shift 2
            ;;
        --)
            shift
            break
            ;;
        -?*)
            echo "unknown option: $1" >&2
            print_usage >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

NODE_QUERY="${1:-}"
if [ "${MODE}" = switch ] && [ -z "${NODE_QUERY}" ]; then
    MODE=help
fi

case "${MODE}" in
    help)
        print_usage
        echo ""
        fetch_proxies || exit 1
        "${PYTHON_BIN}" "${NODES_PY}" list "${SELECTOR_NAME}" < "${PROXIES_JSON}" || exit 1
        exit 1
        ;;
    list)
        fetch_proxies || exit 1
        "${PYTHON_BIN}" "${NODES_PY}" list "${SELECTOR_NAME}" < "${PROXIES_JSON}"
        exit 0
        ;;
    list-all)
        fetch_proxies || exit 1
        "${PYTHON_BIN}" "${NODES_PY}" list-all < "${PROXIES_JSON}"
        exit 0
        ;;
esac

fetch_proxies || exit 1

# The helper resolves an index, an exact name or a unique substring, and writes
# the PUT body so that the shell never has to quote emoji into JSON.
NODE="$("${PYTHON_BIN}" "${NODES_PY}" resolve "${SELECTOR_NAME}" "${NODE_QUERY}" "${REQUEST_JSON}" < "${PROXIES_JSON}")" || exit 1
GROUP_PATH="$("${PYTHON_BIN}" "${NODES_PY}" urlencode "${SELECTOR_NAME}")"
SELECTOR_URL="${CONTROLLER_URL}/proxies/${GROUP_PATH}"

echo "group: ${SELECTOR_NAME}"
echo "switching to: ${NODE}"

HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' -X PUT "${SELECTOR_URL}" \
  -H "Content-Type: application/json" \
  --data-binary "@${REQUEST_JSON}")" || HTTP_CODE="000"

case "${HTTP_CODE}" in
    200|204)
        ;;
    *)
        echo "switch request failed, http status ${HTTP_CODE}" >&2
        exit 1
        ;;
esac

sleep 1
echo "current node:"
if curl -s --max-time 8 "${CONTROLLER_URL}/proxies" -o "${PROXIES_JSON}"; then
    "${PYTHON_BIN}" "${NODES_PY}" now "${SELECTOR_NAME}" < "${PROXIES_JSON}" || true
fi

echo ""
echo "testing proxy..."
curl -s --max-time 10 -x "${PROXY_URL}" https://httpbin.org/ip || echo "proxy test failed"

echo ""
echo "note: normal commands such as 'curl www.google.com' use mihomo only after:"
echo "  . \"${SCRIPT_DIR}/proxy-env.sh\""
