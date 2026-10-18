#!/bin/sh
# Builds "$APP_NAME.app" (default "Voice Control") (overlay + icon), installs it to ~/Applications.
#   scripts/build_app.sh [PROJECT_DIR]   PROJECT_DIR is the code the app will run (a release folder, see release.sh);
#                                        default: this working folder.
# Drag it from there to the Dock. Opening it starts Phoenix, the voice daemon, and the widget.
set -e
cd "$(dirname "$0")/.."
SRC="$PWD"
ROOT="${1:-$PWD}"
NAME="${APP_NAME:-Voice Control}"            # APP_NAME="Prometheus Swarm" builds the separate swarm app
ID_SUFFIX=$(printf '%s' "$NAME" | tr -d ' ' | tr 'A-Z' 'a-z')
APP="$HOME/Applications/$NAME.app"

(cd overlay && swift build -c release 2>&1 | grep -E "error|Compiling|Build complete" || true)
BIN=overlay/.build/release/VoiceOverlay

# icon: render 1024px PNG -> .iconset -> .icns
T=$(mktemp -d)
ICON_ARGS=""
case "$ID_SUFFIX" in *swarm*) ICON_ARGS="--swarm" ;; esac      # the swarm app gets its own icon (a network, not a voice)
"$BIN" --icon "$T/icon.png" $ICON_ARGS > /dev/null
mkdir "$T/AppIcon.iconset"
for s in 16 32 64 128 256 512; do
  sips -z $s $s "$T/icon.png" --out "$T/AppIcon.iconset/icon_${s}x${s}.png" > /dev/null
  sips -z $((s*2)) $((s*2)) "$T/icon.png" --out "$T/AppIcon.iconset/icon_${s}x${s}@2x.png" > /dev/null
done
iconutil -c icns "$T/AppIcon.iconset" -o "$T/AppIcon.icns"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/VoiceControl"
cp "$T/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
printf '%s' "$ROOT" > "$APP/Contents/Resources/project_path"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>com.siddharth.$ID_SUFFIX</string>
  <key>CFBundleExecutable</key><string>VoiceControl</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Prometheus controls your Chrome (the sites you already use) when you ask it to browse for you.</string>
  <key>NSMicrophoneUsageDescription</key><string>$NAME records your voice while you hold Ctrl+F9, Ctrl+F10 or F11.</string>
</dict></plist>
PLIST
# Sign with the stable local certificate when present: macOS ties Accessibility / Input Monitoring /
# Microphone grants to the signing identity, so an ad-hoc signature (new hash every build) loses them.
ID="Voice Control Local Signing"
if security find-identity -p codesigning | grep -q "$ID"; then
  codesign --force --deep --sign "$ID" "$APP" 2>&1 | tail -1
else
  echo "No '$ID' certificate: falling back to ad-hoc signing (permissions will reset on every rebuild)"
  codesign --force --deep --sign - "$APP" 2>&1 | tail -1
fi
rm -rf "$T"
echo "Installed: $APP"
