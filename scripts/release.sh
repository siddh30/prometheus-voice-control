#!/bin/sh
# Freeze the current code as THE version you use (the "stable" profile). Run only when a change is ready for you:
#   scripts/release.sh "what changed"
#
# Why: your app runs from a snapshot, so in-progress edits and test runs here can never change it under you.
# Releases:  ~/Library/Application Support/VoiceControl/releases/<time>/   (last 5 kept; "current" points at the live one)
# Your data: ~/Library/Application Support/VoiceControl/data/             (memory, voice cache, logs: survives every release)
# Roll back: point "current" at an older release and rebuild:  scripts/build_app.sh "<that release folder>"
#   APP_NAME="Prometheus Swarm" PROFILE_NAME=swarm scripts/release.sh "note"   releases the separate swarm app instead
set -e
APP_NAME="${APP_NAME:-Voice Control}"; PROFILE_NAME="${PROFILE_NAME:-stable}"
export APP_NAME
if [ "$PROFILE_NAME" = "swarm" ]; then SUPPORT_NAME="PrometheusSwarm"; PORT=8767; else SUPPORT_NAME="VoiceControl"; PORT=8765; fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"

# Never replace the app while it is open: you may be mid-conversation. (Override only with --force.)
if pgrep -f "$APP_NAME.app/Contents/MacOS/VoiceControl" > /dev/null && [ "$2" != "--force" ]; then
  echo "$APP_NAME is running. Quit it first (or pass --force as the 2nd argument), then run the release again." >&2
  exit 3
fi
[ "$VOICECTL_RELEASE_CHECK_ONLY" = "1" ] && { echo "guard ok"; exit 0; }
BASE="$HOME/Library/Application Support/$SUPPORT_NAME"
TS="$(date +%Y%m%d-%H%M%S)"
REL="$BASE/releases/$TS"
NOTE="${1:-no note}"
mkdir -p "$REL" "$BASE/data" "$BASE/phoenix_data"

echo "1/6 snapshot of the code"
rsync -a --exclude '.venv' --exclude 'data' --exclude 'data-*' --exclude 'phoenix_data' --exclude '.idea' --exclude '__pycache__' \
      --exclude '.pytest_cache' --exclude 'overlay/.build' --exclude 'releases' --exclude '.git' --exclude '*.iml' "$SRC/" "$REL/"
echo "$PROFILE_NAME" > "$REL/profile.txt"

echo "2/6 secrets live once, outside the releases"
[ -f "$BASE/.env" ] || { cp "$SRC/.env" "$BASE/.env"; chmod 600 "$BASE/.env"; }
rm -f "$REL/.env"; ln -s "$BASE/.env" "$REL/.env"

echo "3/6 its own Python environment"
(cd "$REL" && uv sync --python 3.14 --no-dev --quiet)

echo "4/6 your existing memory and voice cache (first release only; the old copy is left untouched)"
if [ ! -f "$BASE/data/voicectl.db" ] && [ -f "$SRC/data/voicectl.db" ]; then
  sqlite3 "$SRC/data/voicectl.db" ".backup '$BASE/data/voicectl.db'"        # safe even while the old app is running
  [ -d "$SRC/data/tts_cache" ] && cp -R "$SRC/data/tts_cache" "$BASE/data/tts_cache"
  [ -d "$SRC/phoenix_data" ] && cp -R "$SRC/phoenix_data/." "$BASE/phoenix_data/" 2>/dev/null || true
  echo "   copied"
else
  echo "   nothing to copy"
fi

echo "5/6 check the snapshot really works as 'stable'"
(cd "$REL" && PROFILE_NAME="$PROFILE_NAME" SUPPORT_NAME="$SUPPORT_NAME" PORT="$PORT" PYTHONPATH=src .venv/bin/python - <<'PY'
import voicectl.main, voicectl.modes.prometheus
from voicectl.core.config import PROFILE, DATA_DIR, load_config
c = load_config()
import os
assert PROFILE == os.environ["PROFILE_NAME"], PROFILE
assert str(DATA_DIR).endswith(os.environ["SUPPORT_NAME"] + "/data"), DATA_DIR
assert c.get("ui.ws_port") == int(os.environ["PORT"]) and c.get("hotkeys.prometheus") == "f10", "installed app must use its own port and the F10 key"
if PROFILE == "swarm":
    assert c.get("wake_word.enabled") is False, "the swarm app has no wake word"
print("   profile stable | data", DATA_DIR, "| port", c.get("ui.ws_port"), "| key", c.get("hotkeys.prometheus"))
PY
)

echo "6/6 the app, pointed at this release"
"$SRC/scripts/build_app.sh" "$REL"

ln -sfn "$REL" "$BASE/current"
echo "$TS  $NOTE" >> "$BASE/RELEASES.log"
ls -1dt "$BASE"/releases/* | tail -n +6 | xargs -I{} rm -rf "{}" 2>/dev/null || true
echo
echo "Released $TS: $NOTE"
echo "Quit the running $APP_NAME (if open) and open it again to use it."
