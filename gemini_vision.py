#!/usr/bin/env python3
"""DEPRECATED — Gemini path was removed from phantom-click.

phantom-click now uses Claude vision only (see `claude_vision.py` and the
ClaudeVision class in phantom.py).

Reasons Gemini was dropped:
  - 30–90s per vision call (web-UI scraping via Playwright + cookies)
  - Off-by-one element identification on dense UI (tabs, grid buttons)
  - `pkill -9 Chromium` between retries — destructive
  - Empty-response failures with no clear retry semantics

This stub exists so old imports don't crash; it always errors.
"""
import sys

if __name__ == "__main__":
    print(
        "ERROR: gemini_vision.py is deprecated. phantom-click uses Claude "
        "vision only — either set ANTHROPIC_API_KEY and use claude_vision.py, "
        "or drive phantom-click interactively from a Claude Code chat where "
        "Claude reads the screenshot and emits click commands.",
        file=sys.stderr,
    )
    sys.exit(2)
