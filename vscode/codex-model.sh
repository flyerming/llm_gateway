#!/usr/bin/env sh
# Switch the Codex model by picking from the private gateway's live model list.
exec python3 "E:\推理加速-2026\新技术\litellm-0914-deploy\vscode\private_api.py" codex --switch-model "$@"
