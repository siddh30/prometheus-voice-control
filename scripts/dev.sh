#!/bin/sh
# Developer-only: run the DEV profile headless (own data, port 8766, no hotkeys / mic / voice / your Chrome).
#   scripts/dev.sh test            run the test suite
#   scripts/dev.sh run             run the daemon in the foreground
#   scripts/dev.sh stop            stop ONLY the dev daemon (never the one you use)
cd "$(dirname "$0")/.." || exit 1
export VOICECTL_PROFILE=dev PYTHONPATH=src
case "$1" in
  test) shift; exec .venv/bin/python -m pytest -q "$@" ;;
  run)  mkdir -p data-dev; echo $$ > data-dev/dev.pid; exec .venv/bin/python -m voicectl.main ;;
  stop) [ -f data-dev/dev.pid ] && kill "$(cat data-dev/dev.pid)" 2>/dev/null && rm -f data-dev/dev.pid && echo "dev daemon stopped" || echo "no dev daemon running" ;;
  *)    echo "usage: scripts/dev.sh test|run|stop"; exit 1 ;;
esac
