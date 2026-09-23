#!/usr/bin/env bash
set -Eeuo pipefail

DATA_DIR="${DSH_DATA_DIR:-/data}"
# 认证资料（htpasswd + UID 映射）放在**独立命名卷**里，不在 /data 下：
# 用户数据卷将来要整体备份或交给别人，密码摘要不该跟着走。目录 0750
# root:www-data，容器内降权后的 agent 连列目录的权限都没有。
AUTH_DIR="${DSH_AUTH_DIR:-/etc/dsh-auth}"
HTPASSWD_FILE="${AUTH_DIR}/htpasswd"
# 老版本把 htpasswd 放在 /data/htpasswd，启动时自动迁走。
LEGACY_HTPASSWD_FILE="${DATA_DIR}/htpasswd"
NGINX_USER="${DSH_NGINX_USER:-www-data}"

mkdir -p "${AUTH_DIR}" "${DATA_DIR}/users" /workspaces

# 目录权限模型（逐个说明，改动前请先读 README「隔离边界」）：
#   /etc/dsh-auth    0750  root:www-data；htpasswd 与 UID 映射都放这里
#   /data            0711  可穿越、不可列举（agent 连 ls /data 都不行）
#   /data/users      0711  同上；每个子目录 0700 且属该用户自己的 UID
#   /workspaces      0711  同上；每个子目录 0700 且属该用户自己的 UID
# 三个 0711 是刻意的：「可穿越、不可列举」保证 Nginx worker 能按已知路径打开
# 文件，但容器里的 agent 无法枚举出「有哪些用户」。
chmod 0711 "${DATA_DIR}" "${DATA_DIR}/users" /workspaces
chown "root:${NGINX_USER}" "${AUTH_DIR}"
chmod 0750 "${AUTH_DIR}"

# 老版本把 htpasswd 直接放在 /data 根下，用户只要 ls /data 就能看见文件名。
# 自动迁移到独立卷；迁移后删掉旧文件，避免遗留一份可被列举的副本。
if [[ -s "${LEGACY_HTPASSWD_FILE}" ]]; then
    if [[ ! -s "${HTPASSWD_FILE}" ]]; then
        echo "start.sh: migrating ${LEGACY_HTPASSWD_FILE} -> ${HTPASSWD_FILE}"
        mv "${LEGACY_HTPASSWD_FILE}" "${HTPASSWD_FILE}"
    else
        echo "start.sh: removing legacy ${LEGACY_HTPASSWD_FILE} (using ${HTPASSWD_FILE})"
        rm -f "${LEGACY_HTPASSWD_FILE}"
    fi
fi

# 老版本的 UID 映射在 /data/.uidmap，同样迁走（新位置与 htpasswd 同卷）。
LEGACY_UIDMAP="${DATA_DIR}/.uidmap"
if [[ -s "${LEGACY_UIDMAP}" && ! -s "${AUTH_DIR}/.uidmap" ]]; then
    echo "start.sh: migrating ${LEGACY_UIDMAP} -> ${AUTH_DIR}/.uidmap"
    mv "${LEGACY_UIDMAP}" "${AUTH_DIR}/.uidmap"
fi

# Nginx 的 worker 进程以非 root 用户（Debian 下是 www-data）运行，而
# auth_basic_user_file 是 worker 在处理「带凭据的请求」时才打开的。
# 因此 htpasswd 必须对 worker 可读：只给 root:www-data 0640，既不暴露给
# 其他用户，又能让 basic auth 生效。文件不存在时跳过（下面会创建）。
fix_htpasswd_perms() {
    [[ -f "${HTPASSWD_FILE}" ]] || return 0
    chown "root:${NGINX_USER}" "${HTPASSWD_FILE}" 2>/dev/null \
        && chmod 0640 "${HTPASSWD_FILE}" \
        || chmod 0644 "${HTPASSWD_FILE}"
}

if [[ -n "${DSH_BOOTSTRAP_USER:-}" && -n "${DSH_BOOTSTRAP_PASSWORD:-}" ]]; then
    tmp="$(mktemp "${AUTH_DIR}/htpasswd.XXXXXX")"
    if [[ -s "${HTPASSWD_FILE}" ]]; then
        cp "${HTPASSWD_FILE}" "${tmp}"
    fi
    htpasswd -B -b "${tmp}" "${DSH_BOOTSTRAP_USER}" "${DSH_BOOTSTRAP_PASSWORD}" >/dev/null
    mv "${tmp}" "${HTPASSWD_FILE}"
    # mktemp 建出来是 0600 root，mv 不改权限；这里立刻修回 worker 可读。
    fix_htpasswd_perms
fi

if [[ ! -s "${HTPASSWD_FILE}" ]]; then
    echo "No users configured. Set DSH_BOOTSTRAP_USER/DSH_BOOTSTRAP_PASSWORD." >&2
    exit 1
fi

# 每次启动都修一遍：htpasswd 存在 named volume 里，早期版本写出的 0600 文件
# 会一直保留，重启时如果只依赖「首次创建」的分支就修不好。
fix_htpasswd_perms

export DSH_DATA_DIR="${DATA_DIR}"
export DSH_AUTH_DIR="${AUTH_DIR}"
export DSH_DEFAULT_MODEL_IDS="${DSH_DEFAULT_MODEL_IDS:-deepseek-v4.1-flash,glm-5.3-flash,qwen3-5-397b}"

python3 /usr/local/bin/dsh-provision.py &
PROVISION_PID=$!

# Wait until the provisioner is actually accepting connections before starting
# Nginx. Otherwise the first requests race against 127.0.0.1:3090 and every
# auth_request fails, which the browser shows as a bare 500.
for _ in $(seq 1 100); do
    if python3 - <<'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.2)
sys.exit(0 if sock.connect_ex(("127.0.0.1", 3090)) == 0 else 1)
PY
    then
        break
    fi
    if ! kill -0 "${PROVISION_PID}" 2>/dev/null; then
        echo "Provisioner exited during startup." >&2
        exit 1
    fi
    sleep 0.1
done

nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
    kill "${NGINX_PID}" "${PROVISION_PID}" 2>/dev/null || true
    wait "${NGINX_PID}" "${PROVISION_PID}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait -n "${NGINX_PID}" "${PROVISION_PID}"
