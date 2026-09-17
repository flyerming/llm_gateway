#!/bin/sh
set -eu

: "${CLIPROXY_API_KEY:?CLIPROXY_API_KEY is required}"

template=/config/config.yaml.template
# The official image starts the binary from /CLIProxyAPI and loads this
# conventional path by default; keep the rendered file inside the container.
runtime=/CLIProxyAPI/config.yaml

if [ ! -r "$template" ]; then
  echo "missing CLIProxyAPI config template: $template" >&2
  exit 1
fi

# CLIProxyAPI intentionally does not expand environment variables inside the
# api-keys YAML field. Render the one data-plane key into a container-local
# config file; OAuth credentials remain persisted separately in the auth volume.
escaped_key=$(printf '%s' "$CLIPROXY_API_KEY" | sed 's/[\\&|]/\\&/g')
sed "s|__CLIPROXY_API_KEY__|$escaped_key|g" "$template" > "$runtime"

exec /CLIProxyAPI/CLIProxyAPI
