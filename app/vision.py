"""Multi-turn Claude vision: ask 'what's the next step' given screenshot + history."""
import base64
import io
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

from PIL import Image

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# Anthropic limit is 5 MB per image. JPEG @ logical resolution stays well under.
_MAX_BYTES = 4_500_000

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("PHANTOM_MODEL", "claude-opus-4-7")
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """You are an autonomous agent driving a {platform_name} computer to complete a task.

Each turn you receive a fresh screenshot of the user's screen ({width}x{height} pixels — the same coordinate space the OS uses for clicks).
Reply with ONLY a JSON object — no prose, no fences. Schema:

{{
  "action": "click" | "double_click" | "type" | "key" | "scroll" | "wait" | "done",
  "x": <int, in the same {width}x{height} space>,    // for click / double_click
  "y": <int>,
  "text": "<string>",               // for type
  "key": "<combo>",                 // for key, e.g. "{primary_mod}+space", "enter", "{primary_mod}+t"
  "direction": "up" | "down",       // for scroll
  "reasoning": "<one short sentence on why>"
}}

Rules:
- Coordinates are in the {width}x{height} pixel space shown in the screenshot. They will be clicked as-is.
- Prefer ONE action per turn and verify in the next screenshot.
- Use {primary_mod}+space (Spotlight) on macOS or the Windows key on Windows to launch apps.
- When the task is complete, return {{"action":"done","reasoning":"<why>"}}.
- If something is unexpected, return {{"action":"wait","reasoning":"<why>"}} and you'll get a fresh screenshot."""


def _platform_strings():
    if sys.platform == "darwin":
        return ("macOS", "cmd")
    if sys.platform == "win32":
        return ("Windows", "ctrl")
    return (sys.platform, "ctrl")


class VisionError(Exception):
    pass


class VisionClient:
    """Holds conversation history so Claude can plan in context."""

    def __init__(self, task: str, screen_size, api_key: str = None):
        self.task = task
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise VisionError("ANTHROPIC_API_KEY not set")
        plat, primary_mod = _platform_strings()
        self.logical_w, self.logical_h = screen_size
        self.system = SYSTEM_PROMPT.format(
            platform_name=plat, width=self.logical_w, height=self.logical_h, primary_mod=primary_mod
        )
        self.history = []  # list of {"role": ..., "content": ...}

    def _encode_image(self, screenshot_path: str) -> tuple[str, str]:
        """Resize screenshot to logical resolution and encode as JPEG.
        Returns (base64_data, media_type). Falls back to PNG if Pillow lacks JPEG."""
        img = Image.open(screenshot_path)
        if img.size != (self.logical_w, self.logical_h):
            img = img.resize((self.logical_w, self.logical_h), Image.LANCZOS)
        # Try JPEG first (much smaller); progressively lower quality if oversize.
        for quality in (90, 80, 70, 60, 50):
            buf = io.BytesIO()
            rgb = img.convert("RGB")
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= _MAX_BYTES:
                return base64.b64encode(data).decode("ascii"), "image/jpeg"
        # Last resort: shrink dimensions further
        scale = 0.75
        while scale > 0.25:
            w = int(self.logical_w * scale)
            h = int(self.logical_h * scale)
            buf = io.BytesIO()
            img.resize((w, h), Image.LANCZOS).convert("RGB").save(
                buf, format="JPEG", quality=60, optimize=True
            )
            data = buf.getvalue()
            if len(data) <= _MAX_BYTES:
                return base64.b64encode(data).decode("ascii"), "image/jpeg"
            scale -= 0.15
        raise VisionError("could not compress screenshot under 5MB")

    def next_action(self, screenshot_path: str) -> dict:
        b64, media_type = self._encode_image(screenshot_path)

        # User turn = task reminder + new screenshot. We keep history concise:
        # only the textual JSON replies from the assistant; never re-send old screenshots.
        user_text = (
            f"Task: {self.task}\n\nWhat's the next single action? Return JSON only."
        )
        new_user_msg = {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": user_text},
            ],
        }
        messages = self.history + [new_user_msg]

        body = {
            "model": MODEL,
            "max_tokens": 512,
            "system": self.system,
            "messages": messages,
        }
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise VisionError(f"http {e.code}: {e.read()[:200].decode('utf-8','ignore')}")
        except Exception as e:
            raise VisionError(str(e))

        text = "".join(
            c.get("text", "") for c in payload.get("content", []) if c.get("type") == "text"
        ).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise VisionError(f"no JSON in response: {text[:200]}")
        try:
            action = json.loads(m.group())
        except json.JSONDecodeError as e:
            raise VisionError(f"invalid JSON: {e} :: {m.group()[:200]}")

        # Record into history WITHOUT the image, to keep token cost flat across long tasks
        self.history.append({
            "role": "user",
            "content": [{"type": "text", "text": "[screenshot omitted] " + user_text}],
        })
        self.history.append({
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps(action, ensure_ascii=False)}],
        })
        return action
