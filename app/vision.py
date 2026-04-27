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

MODEL = os.environ.get("PHANTOM_MODEL", "claude-opus-4-7")
ANTHROPIC_VERSION = "2023-06-01"

# Provider routing — picked from MODEL prefix unless PHANTOM_PROVIDER overrides.
# - anthropic (default for claude-*): https://api.anthropic.com/v1/messages
# - moonshot  (for kimi-* / moonshot-*): https://api.moonshot.ai/v1/chat/completions
#   Kimi K2.5 is OpenAI-compatible, ~25x cheaper than Claude Opus on input,
#   and natively multimodal. Set PHANTOM_MODEL=kimi-k2.5 (or kimi-k2.6) and
#   ANTHROPIC_API_KEY=<your moonshot key> (key env name kept for simplicity).
def _provider() -> str:
    p = os.environ.get("PHANTOM_PROVIDER", "").lower()
    if p in ("anthropic", "moonshot"):
        return p
    if MODEL.startswith(("kimi-", "moonshot-")):
        return "moonshot"
    return "anthropic"

PROVIDER = _provider()
API_URL = (
    "https://api.moonshot.ai/v1/chat/completions" if PROVIDER == "moonshot"
    else "https://api.anthropic.com/v1/messages"
)

SYSTEM_PROMPT = """You are an autonomous agent driving a {platform_name} computer to complete a task.

Each turn you receive a fresh screenshot. The image you see is {width}x{height} pixels. {coord_instructions}

CRITICAL — screenshots are POST-action: this screenshot was taken AFTER your previous action's effect has rendered. So if your previous action was "click the like heart", this screenshot already shows the heart filled red and the count incremented — your click succeeded. Do NOT repeat that action. Instead, BACKTRACK: recognize the prior action is done, and plan the NEXT step from this new state. Same for every action: if you typed text, this screenshot shows the text already typed; if you pressed a key, the navigation already happened; if you clicked a video thumbnail, the video is now loaded. Trust the screenshot — your prior action is already complete.

PROGRESS — every reply MUST include a `progress` field with a one-line running checklist (e.g. `liked: 2/5; on video 3`). The runner echoes your latest progress back to you next turn, so it's your reliable memory across turns. If your prior progress says you finished step X, do NOT redo step X.

TOGGLE-BUTTON DECISION RULE (likes/follows/saves) — applies to every turn where you might click a toggle:

  Step A: Read the COUNT next to the button (e.g. like-count "3701") and write it into `progress` as `last_count: 3701`.
  Step B: On every subsequent turn, BEFORE deciding the action, compare the count NOW visible against the `last_count` in your prior progress.
      - If `count_now != last_count` (it moved by 1 in either direction): your previous click ALREADY TOGGLED the button. The action is COMPLETE. Your next action MUST move on (scroll, key, click a different element, or done) — DO NOT click the toggle again, regardless of what the heart color looks like. Update `last_count` to the new value and increment your task counter (`unliked: 1/3`, etc.).
      - If `count_now == last_count`: the click hasn't landed yet (rare — could be animation lag); you MAY click once more, but ONLY if the count truly hasn't moved. NEVER click 3 turns in a row at the same coords.

  Step C — ABBREVIATED COUNTS (1.4万, 13.5万, 1.2k, 2M, 万+): when the count is shown abbreviated, ±1 changes are INVISIBLE in the display. The count rule cannot help you. In this case, fall back to the TOOLTIP TEXT or BUTTON LABEL on the heart:
      - Douyin: tooltip `点赞` = currently UNLIKED (clicking would Like). tooltip `取消点赞` = currently LIKED (clicking would Unlike).
      - Generic: a button labelled "Like" / "Follow" / "Subscribe" means you are NOT in that state yet. A button labelled "Unlike" / "Following" / "Subscribed" / "Unfollow" means you ARE in that state.
      - After ONE click on an abbreviated-count toggle, ASSUME the click landed (you've already done it once with reasonable accuracy). Your next action MUST move on. Don't keep clicking to verify with no new evidence.

Why this rule exists: heart color/fill is hard to read reliably at small sizes; integer counts are unambiguous when shown precisely. A count change of ±1 is a hard proof your toggle landed. Trust the integer, then the tooltip, then the color.

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
- TOGGLE BUTTONS (like/heart, follow/unfollow, save, mute): clicking again UNDOES the action. Use the COUNT-BASED RULE above — if the visible count moved by 1 since your last `progress` snapshot, the toggle is done; move on regardless of color.
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
    # Repair 3: missing opening quote on a key (e.g. `, y": 475` → `, "y": 475`)
    # — Kimi K2.5 occasionally drops the leading quote.
    c = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\"\s*:", r'\1"\2":', b)
    candidates.append(c)
    # Repair 4: bare (no quotes) keys (e.g. `, y: 475` → `, "y": 475`).
    d = re.sub(r"([{,]\s*)([A-Za-z_]\w*)(\s*:)", r'\1"\2"\3', c)
    candidates.append(d)
    # Repair 5: leading-zero integer literals (e.g. `"y": 093` → `"y": 93`).
    # Strict JSON forbids leading zeros; Kimi K2.5 sometimes pads them. Only
    # rewrite when the leading 0 is followed by a digit and not by `.`.
    e = re.sub(r"(:\s*)0+(\d)", r"\1\2", d)
    candidates.append(e)
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
        # Downsample: long edge capped at _MAX_EDGE for token cost. Coord
        # convention is provider-specific (verified empirically):
        #   - Anthropic Opus 4.7: returns coords in IMAGE space accurately;
        #     runner scales up by 1/scale before dispatch.
        #   - Moonshot Kimi K2.5: ignores the image-space instruction in
        #     complex UI screenshots and returns coords in OS-logical space
        #     directly (UI-automation training prior). Runner DOES NOT scale.
        self.scale = min(1.0, _MAX_EDGE / max(self.logical_w, self.logical_h))
        self.image_w = max(1, round(self.logical_w * self.scale))
        self.image_h = max(1, round(self.logical_h * self.scale))
        if PROVIDER == "moonshot":
            # Kimi K2.5 is unreliable about pixel-space coords on real UI
            # screenshots — returns mixed conventions (image-px vs OS-px vs
            # 0-1 fractions). Force a normalized 0.000-1.000 frame: it's
            # unambiguous and we multiply by logical dims after parsing.
            prompt_w, prompt_h = self.image_w, self.image_h
            coord_instructions = (
                "All x/y coordinates you output are NORMALIZED FRACTIONS in [0, 1]: "
                "(x=0, y=0) is the top-left of the screen, (x=1, y=1) is the bottom-right. "
                "Use up to 3 decimal places (e.g. 0.815). The runner multiplies your fractions "
                "by the OS pixel dimensions automatically — do NOT output raw pixel coordinates "
                "and do NOT include any unit. Examples: heart icon at top-right ≈ (0.92, 0.18); "
                "Spotlight icon in menu bar ≈ (0.97, 0.02)."
            )
        else:
            prompt_w, prompt_h = self.image_w, self.image_h
            coord_instructions = (
                f"All x/y coordinates you output are in this {prompt_w}x{prompt_h} IMAGE pixel "
                "space — measure them off the image directly, the same way you'd describe pixel "
                "positions in any picture. The runner converts them to OS click-space automatically; "
                "you don't need to think about that."
            )
        self.system = SYSTEM_PROMPT.format(
            platform_name=plat, width=prompt_w, height=prompt_h,
            primary_mod=primary_mod, coord_instructions=coord_instructions,
        )
        # Append knowledge base — task-specific procedural tips that the
        # model can't be expected to derive from training (e.g. how Douyin's
        # in-app navigation works). Edit app/knowledge.md to add entries.
        kb_path = os.path.join(os.path.dirname(__file__), "knowledge.md")
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                kb = f.read().strip()
            if kb:
                self.system += "\n\n=== KNOWLEDGE BASE ===\n" + kb
        except FileNotFoundError:
            pass
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
        if PROVIDER == "moonshot":
            # OpenAI-compatible: image_url with data: URI, system as message.
            new_user_msg = {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": user_text},
                ],
            }
            messages = (
                [{"role": "system", "content": self.system}]
                + self.history
                + [new_user_msg]
            )
            body = {
                "model": MODEL,
                "max_tokens": 512,
                # K2.5/K2.6 default to thinking-mode which puts chain-of-thought
                # into `reasoning_content` and leaves `content` empty until the
                # thinking finishes — burns through max_tokens before producing
                # the JSON action. Disable for direct output.
                "thinking": {"type": "disabled"},
                "messages": messages,
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        else:
            # Anthropic Messages API: separate `system`, content blocks with
            # explicit base64 source.
            new_user_msg = {
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": user_text},
                ],
            }
            messages = self.history + [new_user_msg]
            body = {"model": MODEL, "max_tokens": 512,
                    "system": self.system, "messages": messages}
            headers = {"anthropic-version": ANTHROPIC_VERSION,
                       "content-type": "application/json"}
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

        if PROVIDER == "moonshot":
            text = (payload.get("choices", [{}])[0]
                          .get("message", {})
                          .get("content", "") or "").strip()
        else:
            text = "".join(
                c.get("text", "") for c in payload.get("content", []) if c.get("type") == "text"
            ).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise VisionError(f"no JSON in response: {text[:200]}")
        action = _lenient_json_loads(m.group())
        if action is None:
            raise VisionError(f"invalid JSON: {m.group()[:200]}")

        # Coord normalization, provider-specific:
        #  - Anthropic: model returns IMAGE-pixel coords; scale up by 1/scale
        #    to logical OS pixels.
        #  - Moonshot/Kimi: prompt instructs the model to output normalized
        #    0-1 fractions; multiply by logical dims. We also clamp to OS
        #    bounds because Kimi occasionally drifts to pixel-space — if a
        #    value is > 1.5 we treat it as already-pixel and pass through.
        if PROVIDER == "moonshot":
            for k, dim in (("x", self.logical_w), ("y", self.logical_h)):
                v = action.get(k)
                if not isinstance(v, (int, float)):
                    continue
                if 0 <= v <= 1.5:
                    action[k] = round(v * dim)
                else:
                    action[k] = max(0, min(dim - 1, round(v)))
        elif self.scale < 1.0:
            inv = 1.0 / self.scale
            for k in ("x", "y"):
                if isinstance(action.get(k), (int, float)):
                    action[k] = round(action[k] * inv)

        # Capture the model's running progress for echo on next turn.
        prog = action.get("progress")
        if isinstance(prog, str) and prog.strip():
            self._last_progress = prog.strip()

        # Record into history WITHOUT the image (token cost stays flat across
        # long tasks). OpenAI-style providers prefer plain strings for
        # text-only content.
        action_json = json.dumps(action, ensure_ascii=False)
        if PROVIDER == "moonshot":
            self.history.append({"role": "user",
                                 "content": "[screenshot omitted] " + user_text})
            self.history.append({"role": "assistant", "content": action_json})
        else:
            self.history.append({
                "role": "user",
                "content": [{"type": "text", "text": "[screenshot omitted] " + user_text}],
            })
            self.history.append({
                "role": "assistant",
                "content": [{"type": "text", "text": action_json}],
            })
        return action
