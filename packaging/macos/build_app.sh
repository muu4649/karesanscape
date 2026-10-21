#!/usr/bin/env bash
# Mac アプリ「KareSanScape.app」(旧名 OntoMap) を、この Mac の上で作る (手元で作る)
# 保存先は旧名のまま (~/OntoMap)。bundle ID は環境変数 KSS_BUNDLE_ID で変えられる (変えると macOS が別のアプリとみなし、許可を聞き直す)
#   使い方: build_app.sh <ontomap-app コマンドの絶対パス> <作る .app のパス>
# Swift のコンパイラ (Xcode の Command Line Tools) が要る。手元で作るので、Apple の公証は要らない。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CMD="$1"
OUT="$2"
BUNDLE_ID="${KSS_BUNDLE_ID:-io.github.muu4649.karesanscape}"
[ -x "$CMD" ] || { echo "ontomap-app が見つかりません: $CMD" >&2; exit 1; }
VER="$("$CMD" --version)"
# 殻のコードと起動するコマンドが前と同じなら、作り直さない (作り直すと署名が変わり、macOS が別のアプリとみなす)
STAMP="$(cat "$HERE/App.swift" "$HERE/icon.icns" "$HERE/splash_mark.png" | shasum -a 256 | cut -d' ' -f1)-$(printf %s "$CMD" | shasum -a 256 | cut -c1-16)"
if [ -f "$OUT/Contents/Resources/build-stamp" ] && [ "$(cat "$OUT/Contents/Resources/build-stamp")" = "$STAMP" ]; then
  echo "Mac アプリは前のままで使えます: $OUT"
  exit 0
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

swiftc -O -target "$(uname -m)-apple-macos12.0" -o "$TMP/KareSanScape" "$HERE/App.swift"

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$TMP/KareSanScape" "$OUT/Contents/MacOS/KareSanScape"
cp "$HERE/icon.icns" "$OUT/Contents/Resources/icon.icns"
cp "$HERE/splash_mark.png" "$OUT/Contents/Resources/splash_mark.png"
printf %s "$STAMP" > "$OUT/Contents/Resources/build-stamp"
PLIST="$OUT/Contents/Info.plist"
cat > "$PLIST" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string></string>
  <key>CFBundleName</key><string>KareSanScape</string>
  <key>CFBundleDisplayName</key><string>KareSanScape</string>
  <key>CFBundleExecutable</key><string>KareSanScape</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0</string>
  <key>CFBundleVersion</key><string>0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>LXCommand</key><string></string>
</dict>
</plist>
XML
plutil -replace CFBundleShortVersionString -string "$VER" "$PLIST"
plutil -replace CFBundleVersion -string "$VER" "$PLIST"
plutil -replace LXCommand -string "$CMD" "$PLIST"
plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$PLIST"
plutil -lint -s "$PLIST"
codesign --force --sign - "$OUT" >/dev/null 2>&1 || true
echo "作りました: $OUT ($VER)"
