#!/bin/sh
# Local Phoenix (traces UI at http://localhost:6006). Runs as an isolated tool; data stays in ./phoenix_data.
cd "$(dirname "$0")/.." || exit 1
export PHOENIX_WORKING_DIR="${VOICECTL_PHOENIX_DIR:-$PWD/phoenix_data}"
mkdir -p "$PHOENIX_WORKING_DIR"
exec uv tool run --python 3.13 --from arize-phoenix phoenix serve
