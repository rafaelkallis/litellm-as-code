# Development container for litellm-as-code.
#
# Installs the package + dev extras with uv and gives you:
#   - Python 3.12 + uv
#   - pytest wired into VS Code's Test Explorer
#   - Ruff formatting/linting
#
# The container runs as non-root user `vscode`. Applied-state (state.json),
# which holds secrets, lives on a dedicated volume at
# /workspaces/state — do not mount your host state dir into it.

FROM mcr.microsoft.com/devcontainers/python:3.12
