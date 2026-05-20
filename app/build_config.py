"""Build-time configuration baked into the shipped binary.

Only BRIDGE_URL is baked into shipped builds (rewritten by the build scripts
before PyInstaller runs). The bearer token is NO LONGER baked — each user pastes
their own prepaid wallet token into the app at runtime (see app/wallet.py); the
Worker meters that token's dollar balance. BRIDGE_TOKEN remains here only as an
optional dev override (PHANTOM_BRIDGE_TOKEN) so operator-side `python3 -m
app.main` runs can skip the in-app token prompt.

For operator-side dev, env vars PHANTOM_BRIDGE_URL / PHANTOM_BRIDGE_TOKEN
override the placeholders so you can iterate without rebuilding the bundle.

If BRIDGE_URL is not set (neither baked nor env), vision.py falls through to the
old PHANTOM_PROVIDER / GEMINI_API_KEY env-var path so existing direct-Gemini
workflows keep working.
"""
import os

# Build script replaces the @@…@@ markers with literals via sed/replace.
# The unreplaced form ("@@BRIDGE_URL@@") is treated as "not set" at runtime.
_BAKED_BRIDGE_URL = "@@BRIDGE_URL@@"
_BAKED_BRIDGE_TOKEN = "@@BRIDGE_TOKEN@@"


def _resolve(env_var: str, baked: str) -> str | None:
    """Env var wins (for dev). Then the baked literal — only if it's been
    rewritten (i.e. doesn't still look like the placeholder)."""
    v = os.environ.get(env_var, "").strip()
    if v:
        return v
    if baked and not baked.startswith("@@"):
        return baked
    return None


BRIDGE_URL = _resolve("PHANTOM_BRIDGE_URL", _BAKED_BRIDGE_URL)
# Optional dev-only bearer override; production builds leave this None and the
# user-entered wallet token (app/wallet.py) is used instead.
BRIDGE_TOKEN = _resolve("PHANTOM_BRIDGE_TOKEN", _BAKED_BRIDGE_TOKEN)

# True iff this build routes through the operator's bridge — i.e. a CN-ship
# build (BRIDGE_URL baked). The bearer is supplied at runtime by the user, so
# only the URL gates this. Operator-side dev runs without BRIDGE_URL leave this
# False and vision.py falls back to the standard provider routing.
IS_CN_BUILD = bool(BRIDGE_URL)
