#!/usr/bin/env python3
"""Claude vision helper for phantom-click autopilot.

Two modes:
  1. INTERACTIVE (default in Claude Code): do not call this — let Claude in
     the chat read the screenshot and emit click commands directly.
  2. API: set ANTHROPIC_API_KEY. This script POSTs the screenshot to the
     Anthropic Messages API with a deterministic prompt and prints the JSON
     response to stdout. Used by phantom.PhantomAgent.

Usage (API mode):
  python3 claude_vision.py <image-path> <instruction>

Output: a single JSON object {steps:[{action,x,y,text,key,direction,reasoning},...]}.
"""
import base64
import json
import os
import sys
import urllib.request
import urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-4-7"

PROMPT_TMPL = """You are computing pixel coordinates for a phantom-click automation script driving a macOS desktop.

Task: {instruction}

The attached screenshot is the FULL macOS display in Retina pixels. Return click coordinates in those screenshot pixels (the script divides by 2 to get logical points).

Reply with ONLY a JSON object — no prose, no fences:

{{"steps":[
  {{"action":"click","x":<int>,"y":<int>,"reasoning":"<short>"}},
  ...or "type"/"key"/"scroll"/"done"...
]}}

Action types: click, double_click, type (text field), key (e.g. "command+space", "enter"), scroll (direction "up"/"down"), done.

Be conservative: prefer ONE action per response and verify in the next call.
If the task is complete, return {{"steps":[{{"action":"done","reasoning":"<why>"}}]}}.
"""


def main():
    if len(sys.argv) < 3:
        print("Usage: claude_vision.py <image> <instruction>", file=sys.stderr)
        sys.exit(2)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(json.dumps({"action": "error", "reasoning": "ANTHROPIC_API_KEY not set"}))
        sys.exit(0)

    image_path, instruction = sys.argv[1], sys.argv[2]
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": PROMPT_TMPL.format(instruction=instruction)},
            ],
        }],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
        text = "".join(c.get("text", "") for c in payload.get("content", []) if c.get("type") == "text").strip()
        # Extract JSON from response (model may add backticks)
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        print(m.group() if m else json.dumps({"action": "error", "reasoning": "no json in response", "raw": text[:200]}))
    except urllib.error.HTTPError as e:
        print(json.dumps({"action": "error", "reasoning": f"http {e.code}: {e.read()[:200].decode('utf-8','ignore')}"}))
    except Exception as e:
        print(json.dumps({"action": "error", "reasoning": str(e)}))


if __name__ == "__main__":
    main()
