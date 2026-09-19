#!/usr/bin/env bash
# Build a standalone arm64 macOS binary (no Python needed to run genius itself) into dist/standalone/genius/, then smoke-test it.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
uv pip install -q pyinstaller
D="$PWD/src/genius_dev"
pyinstaller --noconfirm --clean --onedir --name genius --paths src \
  --collect-submodules genius_dev --collect-submodules textual --collect-data textual --collect-data rich \
  --hidden-import keyring.backends.macOS --hidden-import keyring.backends.fail \
  --add-data "$D/demo_project:genius_dev/demo_project" --add-data "$D/demo_debug:genius_dev/demo_debug" --add-data "$D/demo_web:genius_dev/demo_web" \
  --distpath dist/standalone --workpath /tmp/pyi-work --specpath /tmp/pyi-spec scripts/entry.py >/dev/null 2>&1
B="$PWD/dist/standalone/genius/genius"
export GENIUS_HOME="$(mktemp -d)"
"$B" --version
"$B" demo --fast | grep -E "^(DONE|INCOMPLETE)"
"$B" demo --scenario debug --fast | grep -E "^(DONE|INCOMPLETE)"
echo "standalone binary: $B ($(du -sh dist/standalone/genius | cut -f1))"
