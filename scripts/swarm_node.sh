#!/bin/sh
# Run this computer as a HEADLESS swarm node: it joins your swarm at login and works in the background, with no app to open.
# Use it on the computers you only want to CONTROL from your main one (where you open Prometheus Swarm).
#   scripts/swarm_node.sh install     start now and at every login
#   scripts/swarm_node.sh status      is it running? (and the last log lines)
#   scripts/swarm_node.sh uninstall   stop and remove
#   scripts/swarm_node.sh print       show the launch file without installing it
# It runs the code of the installed release when there is one (so `scripts/release.sh` updates it), else this folder.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$HOME/Library/Application Support/VoiceControl"
if [ -d "$BASE/current" ]; then ROOT="$BASE/current"; DATA="$BASE/data"; else ROOT="$HERE"; DATA="$HERE/data"; fi
LABEL="com.siddharth.prometheusswarm.node"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UIDN="$(id -u)"

plist() {
cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/.venv/bin/python</string><string>-m</string><string>voicectl.swarm.service</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>$ROOT/src</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DATA/swarm.log</string>
  <key>StandardErrorPath</key><string>$DATA/swarm.log</string>
</dict></plist>
PLIST
}

case "$1" in
  print) plist ;;
  install)
    [ -x "$ROOT/.venv/bin/python" ] || { echo "No Python environment in $ROOT. Run scripts/release.sh first." >&2; exit 1; }
    mkdir -p "$DATA" "$HOME/Library/LaunchAgents"
    plist > "$PLIST"
    launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UIDN" "$PLIST"
    echo "Installed: this computer joins your swarm at every login. Log: $DATA/swarm.log"
    echo "If macOS asks to allow local network access, choose Allow." ;;
  uninstall)
    launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "Removed." ;;
  status)
    if launchctl print "gui/$UIDN/$LABEL" >/dev/null 2>&1; then echo "installed and loaded"; else echo "not running"; fi
    [ -f "$DATA/swarm.log" ] && tail -n 5 "$DATA/swarm.log" || true ;;
  *) echo "usage: scripts/swarm_node.sh install|status|uninstall|print"; exit 1 ;;
esac
