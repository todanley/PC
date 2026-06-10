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


def fetch_trial_token(bridge_url: str, timeout_s: float = 10.0) -> tuple[str | None, str | None]:
    """Hit the bridge's /mint-trial endpoint for a $0.10 trial wallet token.
    Returns (token, error). On success, error is None and the caller should
    `set_token(token)`. On failure, token is None and error names the reason
    (HTTP code, 'trial_already_issued', or the exception class name).

    Used for the first-launch flow in distributed builds: any new user who
    starts the app without a stored token gets a one-shot $0.10 trial so
    they can evaluate the product. The bridge rate-limits to one trial per
    IP per 24h so a single IP can't repeatedly drain the trial pool.

    Network failures degrade silently — the caller falls back to prompting
    for an operator-issued token if the trial fetch fails."""
    import json as _json
    import urllib.request
    import urllib.error
    if not bridge_url:
        return None, "no_bridge_url"
    url = bridge_url.rstrip("/") + "/mint-trial"
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
            tok = (payload.get("token") or "").strip()
            return (tok, None) if tok else (None, "no_token_in_response")
    except urllib.error.HTTPError as e:
        try:
            body = _json.loads(e.read().decode("utf-8"))
            return None, body.get("error") or f"http_{e.code}"
        except Exception:
            return None, f"http_{e.code}"
    except Exception as e:
        return None, type(e).__name__
