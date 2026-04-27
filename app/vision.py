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
_MAX_EDGE = int(os.environ.get("PHANTOM_MAX_EDGE", "1024"))
_JPEG_QUALITY = int(os.environ.get("PHANTOM_JPEG_QUALITY", "75"))

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("PHANTOM_MODEL", "claude-opus-4-7")
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """You are an autonomous agent driving a {platform_name} computer to complete a task.

Each turn you receive a fresh screenshot. The image you see is {width}x{height} pixels. All `x`/`y` coordinates you output are in this {width}x{height} IMAGE pixel space — measure them off the image directly, the same way you'd describe pixel positions in any picture. The runner converts them to OS click-space automatically; you don't need to think about that.

CRITICAL — screenshots are POST-action: this screenshot was taken AFTER your previous action's effect has rendered. So if your previous action was "click the like heart", this screenshot already shows the heart filled red and the count incremented — your click succeeded. Do NOT repeat that action. Instead, BACKTRACK: recognize the prior action is done, and plan the NEXT step from this new state. Same for every action: if you typed text, this screenshot shows the text already typed; if you pressed a key, the navigation already happened; if you clicked a video thumbnail, the video is now loaded. Trust the screenshot — your prior action is already complete.

PROGRESS — every reply MUST include a `progress` field with a one-line running checklist (e.g. `liked: 2/5; on video 3`). The runner echoes your latest progress back to you next turn, so it's your reliable memory across turns. If your prior progress says you finished step X, do NOT redo step X.

Reply with ONLY a JSON object — no prose, no fences. Schema:

{{
  "action": "click" | "double_click" | "type" | "key" | "scroll" | "wait" | "done",
  "x": <int, in the same {width}x{height} space>,    // for click / double_click
  "y": <int>,
  "text": "<string>",               // for type
  "key": "<combo>",                 // for key, e.g. "{primary_mod}+space", "enter", "{primary_mod}+t"
  "direction": "up" | "down",       // for scroll
  "reasoning": "<one short sentence on why this single action>",
  "progress": "<running checklist of what's been completed and what's left, e.g. 'liked: 2/5; on video 3, heart white, about to click'>"
}}

Rules:
- Coordinates are in the {width}x{height} pixel space shown in the screenshot. They will be clicked as-is.
- ONE action per turn. Verify in the next screenshot, update `progress`, plan next.
- Use {primary_mod}+space (Spotlight) on macOS or the Windows key on Windows to launch apps.
- IGNORE unrelated UI on screen — terminals, IDEs, code editors, log panels, other agent windows. Do NOT wait for their spinners ("Waddling…", "Bashing…", build progress, etc.). They are not part of the task; act on the app the task is about.
- If the task names a specific app (e.g. 抖音 / Douyin), prefer launching the desktop app by its native name (type 抖音 in Spotlight, not "Douyin") so you don't end up on a website that requires login.
- If a click on a list item or row didn't navigate, the row label/text or icon is the actual click target, not blank space inside the row.
- TOGGLE BUTTONS (like/heart, follow/unfollow, save, mute): clicking again UNDOES the action. After clicking a toggle, BACKTRACK (the prior action is done — see CRITICAL paragraph above) and your VERY NEXT action must move on (scroll, key, click a different element, or done). NEVER re-click the same toggle "to make sure" — that is guaranteed to undo it.
- When the task is fully complete (per your `progress`), return {{"action":"done","reasoning":"<why>","progress":"<final state>"}}.
- If something is loading, return {{"action":"wait","reasoning":"<why>","progress":"<unchanged>"}} and you'll get a fresh screenshot."""


def _platform_strings():
    if sys.platform == "darwin":
        return ("macOS", "cmd")
    if sys.platform == "win32":
        return ("Windows", "ctrl")
    return (sys.platform, "ctrl")


class VisionError(Exception):
    pass


def _lenient_json_loads(raw: str) -> dict | None:
    """Parse Claude's JSON, tolerating common formatter slips when the prompt
    is in Chinese:
      • smart/typographic quotes (“ ” ‘ ’) → ASCII (")
      • single-quoted keys ('y': 1) → double-quoted ("y": 1)
    Returns the parsed dict, or None if all repairs fail.
    """
    candidates = [raw]
    # Repair 1: smart quotes → ASCII double.
    a = (raw.replace("“", '"').replace("”", '"')
            .replace("‘", "'").replace("’", "'"))
    candidates.append(a)
    # Repair 2: single-quoted JSON keys → double-quoted. Only matches keys that
    # follow `{` or `,` and are plain identifiers; won't touch single quotes
    # inside string values.
    b = re.sub(r"([{,]\s*)'([A-Za-z_]\w*)'(\s*:)", r'\1"\2"\3', a)
    candidates.append(b)
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


class VisionClient:
    """Holds conversation history so Claude can plan in context."""

    def __init__(self, task: str, screen_size, api_key: str = None):
        self.task = task
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise VisionError("ANTHROPIC_API_KEY not set")
        plat, primary_mod = _platform_strings()
        self.logical_w, self.logical_h = screen_size
        self._last_progress: str = ""  # echoed back to the model each turn
        # Downsample: long edge capped at _MAX_EDGE for token cost. The image
        # we send is smaller; we tell the model coords are in IMAGE space and
        # then scale x,y back to logical OS pixels before dispatching. This
        # is empirically reliable to ~1px even at edge=768 — see
        # /tmp/coord_test/test_coord.py for the experiment.
        self.scale = min(1.0, _MAX_EDGE / max(self.logical_w, self.logical_h))
        self.image_w = max(1, round(self.logical_w * self.scale))
        self.image_h = max(1, round(self.logical_h * self.scale))
        self.system = SYSTEM_PROMPT.format(
            platform_name=plat,
            width=self.image_w,
            height=self.image_h,
            primary_mod=primary_mod,
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

    def next_action(self, screenshot_path: str) -> dict:
        b64, media_type = self._encode_image(screenshot_path)

        # User turn = task reminder + last reported progress + new screenshot.
        # Echoing back the model's own `progress` field gives it a reliable
        # checklist memory across turns (image history is dropped to save
        # tokens, so this is the only persistent state the model can rely on).
        progress_line = (
            f"Your reported progress so far: {self._last_progress!r}\n\n"
            if self._last_progress
            else ""
        )
        user_text = (
            f"Task: {self.task}\n\n{progress_line}"
            "What's the next single action? Return JSON only — and remember to update `progress`."
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
        action = _lenient_json_loads(m.group())
        if action is None:
            raise VisionError(f"invalid JSON: {m.group()[:200]}")

        # Model returns coords in IMAGE pixel space; scale up to logical OS
        # pixels for the click dispatcher. (Inverse of self.scale.)
        if self.scale < 1.0:
            inv = 1.0 / self.scale
            for k in ("x", "y"):
                if isinstance(action.get(k), (int, float)):
                    action[k] = round(action[k] * inv)

        # Capture the model's running progress for echo on next turn.
        prog = action.get("progress")
        if isinstance(prog, str) and prog.strip():
            self._last_progress = prog.strip()

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
