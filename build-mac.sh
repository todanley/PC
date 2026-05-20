#!/bin/bash
# Build a CN-shippable macOS .app bundle of phantom-click (噜噜机器人).
#
# Required env:
#   PHANTOM_BRIDGE_URL    — full URL of your bridge tunnel
#                           e.g. https://bridge.yourdomain.com
#   PHANTOM_BRIDGE_TOKEN  — the shared bearer token CN clients use
#                           (mint with: python3 -m bridge.issue_token public-pool --max-per-day 5000)
#
# Output:
#   dist/噜噜机器人.app          ← the bundle (drag to /Applications)
#   dist/噜噜机器人-mac.zip      ← zipped, ready to upload for download
#
# What it does:
#   1. Validates env, checks pyinstaller installed.
#   2. Injects URL+token into app/build_config.py (idempotent — revert on exit).
#   3. Runs pyinstaller via the spec file.
#   4. Ad-hoc signs the bundle so Gatekeeper doesn't reject it outright.
#   5. Zips the .app for distribution.

set -euo pipefail

cd "$(dirname "$0")"

# --- 1. Sanity checks -------------------------------------------------------

# Only the bridge URL is baked now; the bearer token is NOT baked — each user
# pastes their own prepaid wallet token into the app at runtime.
if [[ -z "${PHANTOM_BRIDGE_URL:-}" ]]; then
    cat >&2 <<EOF
error: PHANTOM_BRIDGE_URL must be set.

Example:
  export PHANTOM_BRIDGE_URL=https://bridge.yourdomain.com
  ./build-mac.sh

(Issue user wallet tokens separately with worker/issue-token.sh.)
EOF
    exit 1
fi

if [[ "$PHANTOM_BRIDGE_URL" != https://* && "$PHANTOM_BRIDGE_URL" != http://127.0.0.1* ]]; then
    echo "error: PHANTOM_BRIDGE_URL must be https:// (or http://127.0.0.1:* for local testing)" >&2
    exit 1
fi

if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "→ installing PyInstaller …"
    pip3 install -q pyinstaller
fi

# Pillow / PySide6 / pyautogui must be importable so PyInstaller can find them.
python3 -c "import PySide6, PIL, pyautogui, Quartz" 2>/dev/null || {
    echo "error: app deps missing. run: pip3 install -r requirements-app.txt" >&2
    exit 1
}

# --- 2. Inject config -------------------------------------------------------

CFG=app/build_config.py
BACKUP="$(mktemp -t build_config.XXXXXX.py.bak)"
cp "$CFG" "$BACKUP"

restore_config() {
    if [[ -f "$BACKUP" ]]; then
        mv "$BACKUP" "$CFG"
        echo "→ restored $CFG"
    fi
}
trap restore_config EXIT INT TERM

# sed-replace the @@…@@ placeholders. We use a non-/ delimiter (|) because
# URLs contain slashes; and we escape any | that would somehow appear in
# the token (unlikely — secrets.token_urlsafe yields [A-Za-z0-9_-]).
URL_ESC=$(printf '%s\n' "$PHANTOM_BRIDGE_URL" | sed -e 's/[\&|]/\\&/g')
# Token left as the @@BRIDGE_TOKEN@@ placeholder on purpose → build_config
# resolves it to None; users supply their own wallet token.
sed -i.bak \
    -e "s|@@BRIDGE_URL@@|$URL_ESC|g" \
    "$CFG"
rm -f "${CFG}.bak"

# Verify the substitution actually happened (sed silently no-ops on missed
# patterns and we'd ship a broken bundle otherwise).
if grep -q '@@BRIDGE_URL@@' "$CFG"; then
    echo "error: BRIDGE_URL placeholder still present in $CFG after substitution" >&2
    exit 1
fi
echo "→ build_config baked: BRIDGE_URL=$PHANTOM_BRIDGE_URL (no token baked; user enters wallet token)"

# --- 3. PyInstaller ---------------------------------------------------------

# Clean any stale build artifacts so a previous failed run can't pollute.
rm -rf build dist

echo "→ running PyInstaller (this takes 1-3 min on first build) …"
python3 -m PyInstaller phantom-click.spec --noconfirm --clean

APP="dist/噜噜机器人.app"
if [[ ! -d "$APP" ]]; then
    echo "error: PyInstaller didn't produce $APP" >&2
    exit 1
fi

# --- 4. Ad-hoc sign + permission entitlements --------------------------------

# Without ANY signature, Gatekeeper outright refuses to launch the bundle on
# many macOS versions (says "is damaged and can't be opened"). Ad-hoc
# signature (identity = "-") satisfies Gatekeeper without needing a Developer
# ID — users still see "Apple cannot check it" on first open, which we tell
# them to bypass via right-click → Open.
#
# Apple Silicon gotcha: python.org's Python framework ships pre-signed by
# Apple (with their Team ID). When we ad-hoc sign the outer binary, dyld
# refuses to load the inner Python because Team IDs mismatch. Fix: strip
# every signature inside the bundle, then re-sign each Mach-O bottom-up.
# Order matters — must sign nested frameworks BEFORE their parent.
echo "→ stripping vendored signatures inside $APP …"
find "$APP" \( -name "*.so" -o -name "*.dylib" \) -exec \
    codesign --remove-signature {} + 2>/dev/null || true
find "$APP" -path "*/Frameworks/*" -type f \
    \( -perm -u=x -o -name "Python" -o -name "QtCore" -o -name "QtGui" -o -name "QtWidgets" \) \
    -exec codesign --remove-signature {} + 2>/dev/null || true

echo "→ ad-hoc re-signing $APP (depth-first) …"
# Sign every dynamic library / shared object first.
find "$APP" \( -name "*.so" -o -name "*.dylib" \) -print0 \
    | xargs -0 codesign --force --sign - --timestamp=none 2>/dev/null

# Sign each Python.framework Version's binary.
find "$APP" -path "*/Python.framework/Versions/*/Python" -print0 \
    | xargs -0 -I{} codesign --force --sign - --timestamp=none "{}" 2>/dev/null || true

# Sign each Qt framework binary inside the bundle.
find "$APP" -path "*/Qt*.framework/Versions/*/Qt*" -type f -print0 \
    | xargs -0 -I{} codesign --force --sign - --timestamp=none "{}" 2>/dev/null || true

# Finally sign the outer bundle with --deep so anything missed gets covered.
codesign --force --deep --sign - --timestamp=none "$APP" 2>&1 | tail -3

# Verify Gatekeeper will accept it (still warns the user, but won't reject).
echo "→ verifying signature …"
codesign --verify --verbose=1 "$APP" 2>&1 | tail -3 || true

# --- 5. Zip for distribution ------------------------------------------------

cd dist
ZIP="噜噜机器人-mac.zip"
rm -f "$ZIP"
# `ditto` preserves macOS extended attributes (signature, quarantine flags)
# which `zip` would strip and ruin the bundle.
ditto -c -k --sequesterRsrc --keepParent "噜噜机器人.app" "$ZIP"
cd ..

SIZE=$(du -sh "dist/噜噜机器人-mac.zip" | awk '{print $1}')
APP_SIZE=$(du -sh "$APP" | awk '{print $1}')

cat <<EOF

=============================================================
  build OK
=============================================================
  bundle      : $APP                ($APP_SIZE)
  distribution: dist/$ZIP   ($SIZE)
  baked URL   : $PHANTOM_BRIDGE_URL

  CN user flow:
    1. Download dist/$ZIP
    2. Unzip → drag 噜噜机器人.app to /Applications
    3. First open: right-click → 打开 (bypasses Gatekeeper warning once)
    4. Grant Screen Recording + Accessibility when prompted
    5. Type task, press 运行
=============================================================
EOF
