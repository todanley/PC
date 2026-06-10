"""Local store for the user's prepaid wallet token.

The shipped app no longer bakes a shared bearer token. Instead the operator
sells each user a token (see worker/issue-token.sh) that carries a dollar
balance; the user pastes it in once and the app reuses it until the balance is
exhausted (HTTP 402), then prompts for a new one.

Persistence mirrors humanize.TaskCounter: a tiny JSON file under the per-user
app-data dir (%LOCALAPPDATA%\\PhantomClick on Windows, ~/Library/Application
Support/PhantomClick on macOS). Best-effort — failures degrade to "no token"
rather than raising.
"""
import json
import os
from pathlib import Path


def _path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA",
                                   str(Path.home() / "AppData" / "Local")))
    else:
        base = Path.home() / "Library" / "Application Support"
    return base / "PhantomClick" / "token.json"


def get_token() -> str | None:
    """Return the stored token, or None if not set / unreadable."""
    try:
        tok = json.loads(_path().read_text()).get("token")
        tok = (tok or "").strip()
        return tok or None
    except Exception:
        return None


def set_token(token: str) -> None:
    """Persist the token (trimmed). Silently no-ops on write failure."""
    token = (token or "").strip()
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"token": token}))
    except Exception:
        pass


def clear_token() -> None:
    """Forget the stored token (e.g. after it's exhausted)."""
    try:
        _path().unlink(missing_ok=True)
    except Exception:
        pass


