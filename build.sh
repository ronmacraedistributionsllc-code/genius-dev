#!/usr/bin/env bash
# Build the wheel + sdist into ./dist, then install the *actual* wheel into a clean pipx environment and exercise the CLI.
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "uv is required: brew install uv"; exit 1; }
rm -rf dist
uv build
WHEEL="$(ls dist/genius_dev-*.whl | head -1)"
echo "wheel: $WHEEL"
tmp="$(mktemp -d)"
export PIPX_HOME="$tmp/pipx" PIPX_BIN_DIR="$tmp/bin" GENIUS_HOME="$tmp/home"
PIPX="pipx"; command -v pipx >/dev/null || PIPX="uvx pipx"
$PIPX install --python "$(command -v python3.13 || command -v python3)" "$WHEEL" >/dev/null
GENIUS="$tmp/bin/genius"
show() { "$@" > "$tmp/out.txt" 2>&1 || { cat "$tmp/out.txt"; echo "FAILED: $*"; exit 1; }; head -"$N" "$tmp/out.txt"; }
N=1;  show "$GENIUS" --version
N=4;  show "$GENIUS" --help
N=12; show "$GENIUS" models
N=3;  show "$GENIUS" plugins
proj="$tmp/proj"; mkdir -p "$proj" && printf 'def f():\n    return 1\n' > "$proj/a.py" && (cd "$proj" && git init -q)
cd "$proj"
N=3;  show "$GENIUS" status
N=14; show "$GENIUS" doctor --quick
show "$GENIUS" demo --fast >/dev/null && echo "demo: OK"
cd - >/dev/null
echo "installed from $WHEEL and verified (pipx, clean environment). Real install:  pipx install $WHEEL"
