#!/bin/sh
# Starts everything: Phoenix (traces, http://localhost:6006), the overlay widget, and the voice daemon.
# (Runs the daemon via PYTHONPATH rather than the editable install: macOS keeps flagging that .pth as hidden.)
cd "$(dirname "$0")" || exit 1
mkdir -p data

if ! curl -s -o /dev/null http://localhost:6006/healthz; then
  scripts/phoenix.sh > data/phoenix.log 2>&1 &
  echo "Phoenix starting at http://localhost:6006 (log: data/phoenix.log)"
fi

if [ ! -x overlay/.build/release/VoiceOverlay ]; then
  (cd overlay && swift build -c release 2>&1 | tail -1)
fi
pkill -f "overlay/.build/release/VoiceOverlay" 2>/dev/null
overlay/.build/release/VoiceOverlay > data/overlay.log 2>&1 &
OVERLAY=$!
trap 'kill $OVERLAY 2>/dev/null' EXIT INT TERM

PYTHONPATH=src .venv/bin/python -m voicectl.main "$@"
