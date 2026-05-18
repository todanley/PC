"""Build-time configuration baked into the shipped binary.

Two literals get rewritten by `build-mac.sh` before PyInstaller runs:
  • BRIDGE_URL    — the public URL of the operator's bridge tunnel
  • BRIDGE_TOKEN  — the shared bearer token CN clients use to call the bridge

For operator-side dev (running `python3 -m app.main` directly), env vars
PHANTOM_BRIDGE_URL / PHANTOM_BRIDGE_TOKEN override these placeholders so you
can iterate without rebuilding the bundle.

If neither the baked literal nor the env var is set, vision.py falls through
to the old PHANTOM_PROVIDER / GEMINI_API_KEY env-var path so existing
direct-Gemini workflows keep working.
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
BRIDGE_TOKEN = _resolve("PHANTOM_BRIDGE_TOKEN", _BAKED_BRIDGE_TOKEN)

# True iff this build has both pieces wired up — i.e. it's a CN-ship build.
# Operator-side dev runs (env vars empty + placeholders unreplaced) leave
# this False and vision.py falls back to the standard provider routing.
IS_CN_BUILD = bool(BRIDGE_URL and BRIDGE_TOKEN)
