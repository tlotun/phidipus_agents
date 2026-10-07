#!/usr/bin/env bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
# ══════════════════════════════════════════════════════════════════════
#  Build dist/Phidipus-Installer.pkg — the most "one click" option:
#  double-click → standard macOS installer → Terminal opens and finishes
#  the setup (models are downloaded with a visible progress bar).
#
#  Without an Apple Developer ID the pkg works but macOS warns on first open.
#  With one, sign + notarize so it opens without warnings:
#    installer/build_pkg.sh --sign "Developer ID Installer: Your Name (TEAMID)" \
#                           --notarize-profile phidipus-notary
#  (create the profile once: xcrun notarytool store-credentials phidipus-notary
#      --apple-id you@example.com --team-id TEAMID --password <app-specific password>)
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIGN=""; PROFILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sign) SIGN="$2"; shift 2 ;;
        --notarize-profile) PROFILE="$2"; shift 2 ;;
        *) echo "unknown option $1"; exit 1 ;;
    esac
done
VERSION="$(python3 -c "import json;print(json.load(open('$ROOT/installer/manifest.json'))['release'])")"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
PAYLOAD="$STAGE/root/Library/Application Support/Phidipus/Phidipus"
mkdir -p "$PAYLOAD" "$STAGE/scripts" "$ROOT/dist"

# release files only (respects .gitignore: no config, keys, data, models, venv)
"$ROOT/venv/bin/python" - "$ROOT" "$PAYLOAD" <<'PY'
import shutil, sys
from pathlib import Path
root, dest = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from tools.release_files import release_files
for f in release_files():
    target = dest / f.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(f, target)
PY

cat > "$STAGE/scripts/postinstall" <<'POST'
#!/bin/bash
# runs as root: copy to the logged-in user's ~/Applications and let the user finish in Terminal
USER_NAME="$(stat -f%Su /dev/console)"
[[ -z "$USER_NAME" || "$USER_NAME" == "root" ]] && exit 0
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"
DEST="$USER_HOME/Applications/Phidipus"
mkdir -p "$DEST"
rsync -a --exclude 'config.yaml' --exclude 'keys/' --exclude 'data/' --exclude 'venv/' \
      "/Library/Application Support/Phidipus/Phidipus/" "$DEST/"
chown -R "$USER_NAME" "$DEST" "$USER_HOME/Applications"
chmod +x "$DEST"/*.command "$DEST/install.sh"
launchctl asuser "$(id -u "$USER_NAME")" sudo -u "$USER_NAME" open -a Terminal "$DEST/Cai_Dat_Phidipus.command"
exit 0
POST
chmod +x "$STAGE/scripts/postinstall"

xattr -cr "$STAGE/root" "$STAGE/scripts" 2>/dev/null || true   # protected attrs (com.apple.provenance) may remain as ._ entries; Installer re-applies them as xattrs
export COPYFILE_DISABLE=1
pkgbuild --root "$STAGE/root" --scripts "$STAGE/scripts" --identifier com.phidipus.installer \
         --version "$VERSION" --install-location / "$STAGE/component.pkg"
OUT="$ROOT/dist/Phidipus-Agents-Installer-$VERSION.pkg"
if [[ -n "$SIGN" ]]; then
    productbuild --package "$STAGE/component.pkg" --sign "$SIGN" "$OUT"
else
    productbuild --package "$STAGE/component.pkg" "$OUT"
    echo "! Gói CHƯA ký — macOS sẽ cảnh báo khi mở (cần Apple Developer ID để ký)."
fi
if [[ -n "$PROFILE" ]]; then
    xcrun notarytool submit "$OUT" --keychain-profile "$PROFILE" --wait
    xcrun stapler staple "$OUT"
fi
echo "✓ $OUT"
