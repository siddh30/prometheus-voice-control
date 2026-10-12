#!/bin/sh
# Builds and installs "Prometheus Switch.app" (~/Applications): a menu-bar app, no Dock icon, with one job. While it is running, F12
# (anywhere) or the button on its card quits whichever of Voice Control / Prometheus Swarm is running and opens the other. It has no
# daemon and needs no release: run this once (again after changing its code). The first time, macOS asks for Accessibility and Input
# Monitoring (for the F12 key); until you allow them, F12 still works while the card is open.
set -e
cd "$(dirname "$0")/.."
APP="$HOME/Applications/Prometheus Switch.app"

(cd overlay && swift build -c release 2>&1 | grep -E "error|Build complete" || true)
BIN=overlay/.build/release/PrometheusSwitch
[ -x "$BIN" ] || { echo "Build failed: no $BIN" >&2; exit 1; }

T=$(mktemp -d)
overlay/.build/release/VoiceOverlay --icon "$T/icon.png" --switch > /dev/null
mkdir "$T/AppIcon.iconset"
for s in 16 32 64 128 256 512; do
  sips -z $s $s "$T/icon.png" --out "$T/AppIcon.iconset/icon_${s}x${s}.png" > /dev/null
  sips -z $((s*2)) $((s*2)) "$T/icon.png" --out "$T/AppIcon.iconset/icon_${s}x${s}@2x.png" > /dev/null
done
iconutil -c icns "$T/AppIcon.iconset" -o "$T/AppIcon.icns"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/PrometheusSwitch"
cp "$T/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Prometheus Switch</string>
  <key>CFBundleDisplayName</key><string>Prometheus Switch</string>
  <key>CFBundleIdentifier</key><string>com.siddharth.prometheusswitch</string>
  <key>CFBundleExecutable</key><string>PrometheusSwitch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
# the same stable local certificate as the other apps
ID="Voice Control Local Signing"
if security find-identity -p codesigning | grep -q "$ID"; then
  codesign --force --deep --sign "$ID" "$APP" 2>&1 | tail -1
else
  echo "No '$ID' certificate: ad-hoc signing (permissions will reset on every rebuild)"
  codesign --force --deep --sign - "$APP" 2>&1 | tail -1
fi
rm -rf "$T"
echo "Installed: $APP"
