#!/bin/sh
# Source this file to route the current shell through mihomo.
# Usage: . ./proxy-env.sh

PROXY_URL="${MIHOMO_PROXY_URL:-http://127.0.0.1:7890}"
SOCKS_URL="${MIHOMO_SOCKS_URL:-socks5h://127.0.0.1:7890}"
NO_PROXY_LIST="${MIHOMO_NO_PROXY:-localhost,127.0.0.1,::1}"

export http_proxy="${PROXY_URL}"
export https_proxy="${PROXY_URL}"
export all_proxy="${SOCKS_URL}"
export HTTP_PROXY="${PROXY_URL}"
export HTTPS_PROXY="${PROXY_URL}"
export ALL_PROXY="${SOCKS_URL}"
export no_proxy="${NO_PROXY_LIST}"
export NO_PROXY="${NO_PROXY_LIST}"

echo "shell proxy enabled"
echo "http_proxy=${http_proxy}"
echo "https_proxy=${https_proxy}"
echo "all_proxy=${all_proxy}"
echo "no_proxy=${no_proxy}"
