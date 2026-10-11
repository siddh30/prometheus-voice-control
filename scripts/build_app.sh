#!/bin/sh
# Builds "Voice Control.app" (overlay + icon), installs it to ~/Applications.
#   scripts/build_app.sh [PROJECT_DIR]   PROJECT_DIR is the code the app will run (a release folder, see release.sh);
#                                        default: this working folder.
# Drag it from there to the Dock. Opening it starts Phoenix, the voice daemon, and the widget.
set -e
cd "$(dirname "$0")/.."
SRC="$PWD"
ROOT="${1:-$PWD}"
APP="$HOME/Applications/Voice Control.app"

(cd overlay && swift build -c release 2>&1 | grep -E "error|Compiling|Build complete" || true)
BIN=overlay/.build/release/VoiceOverlay

# icon: render 1024px PNG -> .iconset -> .icns
T=$(mktemp -d)
"$BIN" --icon "$T/icon.png" > /dev/null
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
  <key>CFBundleName</key><string>Voice Control</string>
  <key>CFBundleDisplayName</key><string>Voice Control</string>
  <key>CFBundleIdentifier</key><string>com.siddharth.voicecontrol</string>
  <key>CFBundleExecutable</key><string>VoiceControl</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Prometheus controls your Chrome (the sites you already use) when you ask it to browse for you.</string>
  <key>NSMicrophoneUsageDescription</key><string>Voice Control records your voice while you hold F9, F10 or F11.</string>
  <key>NSLocalNetworkUsageDescription</key><string>Prometheus Swarm connects your computers to each other on your home network.</string>
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

# Prometheus Swarm is a mode of this app now (the Swarm / Single switch in the widget): remove the separate app an earlier build installed.
rm -rf "$HOME/Applications/Prometheus Swarm.app"
rm -rf "$T"
echo "Installed: $APP"
