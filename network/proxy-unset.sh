#!/bin/sh
# Source this file to remove proxy variables from the current shell.
# Usage: . ./proxy-unset.sh

unset http_proxy
unset https_proxy
unset all_proxy
unset HTTP_PROXY
unset HTTPS_PROXY
unset ALL_PROXY

NO_PROXY_LIST="${MIHOMO_NO_PROXY:-localhost,127.0.0.1,::1}"
export no_proxy="${NO_PROXY_LIST}"
export NO_PROXY="${NO_PROXY_LIST}"

echo "shell proxy disabled"
echo "no_proxy=${no_proxy}"
