#!/usr/bin/env bash
set -Eeuo pipefail

DATA_DIR="${DSH_DATA_DIR:-/data}"
HTPASSWD_FILE="${DATA_DIR}/htpasswd"

mkdir -p "${DATA_DIR}/users" /workspaces
chmod 0700 "${DATA_DIR}/users" /workspaces

if [[ -n "${DSH_BOOTSTRAP_USER:-}" && -n "${DSH_BOOTSTRAP_PASSWORD:-}" ]]; then
    tmp="$(mktemp "${DATA_DIR}/htpasswd.XXXXXX")"
    if [[ -s "${HTPASSWD_FILE}" ]]; then
        cp "${HTPASSWD_FILE}" "${tmp}"
    fi
    htpasswd -B -b "${tmp}" "${DSH_BOOTSTRAP_USER}" "${DSH_BOOTSTRAP_PASSWORD}" >/dev/null
    mv "${tmp}" "${HTPASSWD_FILE}"
    chmod 0600 "${HTPASSWD_FILE}"
fi

if [[ ! -s "${HTPASSWD_FILE}" ]]; then
    echo "No users configured. Set DSH_BOOTSTRAP_USER/DSH_BOOTSTRAP_PASSWORD." >&2
    exit 1
fi

export DSH_DATA_DIR="${DATA_DIR}"
export DSH_DEFAULT_MODEL_IDS="${DSH_DEFAULT_MODEL_IDS:-deepseek-v4.1-flash,glm-5.3-flash,qwen3-5-397b}"

python3 /usr/local/bin/dsh-provision.py &
PROVISION_PID=$!

nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
    kill "${NGINX_PID}" "${PROVISION_PID}" 2>/dev/null || true
    wait "${NGINX_PID}" "${PROVISION_PID}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait -n "${NGINX_PID}" "${PROVISION_PID}"
