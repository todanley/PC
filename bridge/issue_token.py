"""Mint a new bearer token for the bridge.

  python -m bridge.issue_token <label> [--max-per-day N]

Prints the token to stdout — copy it to the CN client. Token is 24 bytes of
url-safe randomness, prefixed `pc_` so it's grep-able in logs.

--max-per-day caps the calls the holder can make per UTC day. Omit (or pass
0) for unlimited; default is 500 so a stolen / leaked token can't drain the
operator's Gemini bill silently.
"""
import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

TOKENS_FILE = Path(__file__).resolve().parent / "tokens.json"
DEFAULT_MAX_PER_DAY = 500


def main():
    p = argparse.ArgumentParser()
    p.add_argument("label", help="human-readable label for the token (logged on every call)")
    p.add_argument(
        "--max-per-day", type=int, default=DEFAULT_MAX_PER_DAY,
        help=f"max upstream calls per UTC day; 0 = unlimited (default {DEFAULT_MAX_PER_DAY})",
    )
    args = p.parse_args()
    label = args.label.strip()
    if not label:
        p.error("label must be non-empty")
    if args.max_per_day < 0:
        p.error("--max-per-day must be >= 0")

    tokens = {}
    if TOKENS_FILE.exists():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
        except json.JSONDecodeError:
            p.error(f"{TOKENS_FILE} is corrupt — refusing to overwrite")

    token = "pc_" + secrets.token_urlsafe(24)
    tokens[token] = {
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # 0 → unlimited; the server treats `max_calls_per_day` only as a cap
        # when it's a positive int.
        "max_calls_per_day": args.max_per_day if args.max_per_day > 0 else None,
        "calls_today": 0,
        "calls_total": 0,
        "day": datetime.now(timezone.utc).date().isoformat(),
    }
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2, ensure_ascii=False))
    print(token)


if __name__ == "__main__":
    main()
