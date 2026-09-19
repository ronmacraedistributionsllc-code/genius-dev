#!/usr/bin/env bash
# Local development: create venv, install editable with dev + browser extras, run genius (or any command).
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  command -v uv >/dev/null || { echo "uv is required: brew install uv"; exit 1; }
  uv venv --python 3.13 .venv
fi
source .venv/bin/activate
uv pip install -q -e ".[dev,browser]"
if [ "${1:-}" = "test" ]; then shift; exec python -m pytest "$@"; fi
if [ "${1:-}" = "shots" ]; then shift; exec python scripts/tui_shot.py "$@"; fi
exec genius "$@"
