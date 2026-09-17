@echo off
rem Switch the Codex model by picking from the private gateway's live model list.
python "E:\推理加速-2026\新技术\litellm-0914-deploy\vscode\private_api.py" codex --switch-model %*
pause
