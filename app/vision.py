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
# Token cost on Anthropic vision is ~ (w*h)/750. Capping the long edge well below
# the typical retina-logical resolution (e.g. 1728x1117 -> 1280x827) cuts vision
# tokens by ~2x at quality the model can still read UI text from.
_MAX_EDGE = int(os.environ.get("PHANTOM_MAX_EDGE", "1280"))
_JPEG_QUALITY = int(os.environ.get("PHANTOM_JPEG_QUALITY", "75"))

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
- IGNORE unrelated UI on screen — terminals, IDEs, code editors, log panels, other agent windows. Do NOT wait for their spinners ("Waddling…", "Bashing…", build progress, etc.). They are not part of the task; act on the app the task is about.
- If the task names a specific app (e.g. 抖音 / Douyin), prefer launching the desktop app by its native name (type 抖音 in Spotlight, not "Douyin") so you don't end up on a website that requires login.
- If a click on a list item or row didn't navigate, the row label/text or icon is the actual click target, not blank space inside the row.
- TOGGLE BUTTONS (like/heart, follow/unfollow, save, mute): clicking again UNDOES the action. After you successfully like/follow/save something, NEVER click that same button again — your next action must move on (scroll, click a different element, or done). If unsure whether the click registered, scroll first to advance, not re-click.
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
        # Downsample target: long edge capped at _MAX_EDGE for token cost.
        self.scale = min(1.0, _MAX_EDGE / max(self.logical_w, self.logical_h))
        self.image_w = max(1, round(self.logical_w * self.scale))
        self.image_h = max(1, round(self.logical_h * self.scale))
        self.system = SYSTEM_PROMPT.format(
            platform_name=plat, width=self.image_w, height=self.image_h, primary_mod=primary_mod
        )
        self.history = []  # list of {"role": ..., "content": ...}

    def _encode_image(self, screenshot_path: str) -> tuple[str, str]:
        """Resize screenshot to the downsampled (image_w x image_h) target and
        encode as JPEG. Returns (base64_data, media_type)."""
        img = Image.open(screenshot_path)
        if img.size != (self.image_w, self.image_h):
            img = img.resize((self.image_w, self.image_h), Image.LANCZOS)
        rgb = img.convert("RGB")
        for quality in (_JPEG_QUALITY, 65, 55, 45):
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= _MAX_BYTES:
                return base64.b64encode(data).decode("ascii"), "image/jpeg"
        # Last resort: shrink dimensions further (rare — only triggers for huge
        # logical resolutions where even quality=45 at the cap is over 5MB).
        s = 0.75
        while s > 0.25:
            w = max(1, int(self.image_w * s))
            h = max(1, int(self.image_h * s))
            buf = io.BytesIO()
            rgb.resize((w, h), Image.LANCZOS).save(buf, format="JPEG", quality=55, optimize=True)
            data = buf.getvalue()
            if len(data) <= _MAX_BYTES:
                return base64.b64encode(data).decode("ascii"), "image/jpeg"
            s -= 0.15
        raise VisionError("could not compress screenshot under 5MB")

    def next_action(self, screenshot_path: str, feedback: str | None = None) -> dict:
        b64, media_type = self._encode_image(screenshot_path)

        # User turn = task reminder + optional runner feedback + new screenshot.
        # History keeps only text JSON replies; never re-send old screenshots.
        parts = []
        if feedback:
            parts.append(feedback)
        parts.append(f"Task: {self.task}")
        parts.append("What's the next single action? Return JSON only.")
        user_text = "\n\n".join(parts)
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
        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self.api_key.startswith("sk-ant-oat"):
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        else:
            headers["x-api-key"] = self.api_key
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"), headers=headers,
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

        # Map x,y from the downsampled image space back to logical screen pixels.
        if self.scale < 1.0:
            inv = 1.0 / self.scale
            for k in ("x", "y"):
                if isinstance(action.get(k), (int, float)):
                    action[k] = round(action[k] * inv)

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
