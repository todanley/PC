"""Multi-turn Claude vision: ask 'what's the next step' given screenshot + history."""
import base64
import io
import json
import os
import re
import ssl
import subprocess
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
# Default to RETINA resolution (3456 on the user's logical-1728 display) so
# the model has enough pixels for small UI targets like Douyin's 已关注
# buttons or back arrows. JPEG encoder still falls back to a smaller size
# if the file would exceed the API's per-image byte cap.
_MAX_EDGE = int(os.environ.get("PHANTOM_MAX_EDGE", "3456"))
_JPEG_QUALITY = int(os.environ.get("PHANTOM_JPEG_QUALITY", "75"))

MODEL = os.environ.get("PHANTOM_MODEL", "claude-opus-4-7")
ANTHROPIC_VERSION = "2023-06-01"

# Provider routing — picked from MODEL prefix unless PHANTOM_PROVIDER overrides.
# - anthropic (default for claude-*): https://api.anthropic.com/v1/messages
# - moonshot  (for kimi-* / moonshot-*): https://api.moonshot.ai/v1/chat/completions
#   Kimi K2.5 is OpenAI-compatible, ~25x cheaper than Claude Opus on input,
#   and natively multimodal. Set PHANTOM_MODEL=kimi-k2.5 (or kimi-k2.6) and
#   ANTHROPIC_API_KEY=<your moonshot key> (key env name kept for simplicity).
# - gemini    (for gemini-*): no HTTP API key — drives gemini.google.com via
#   the Playwright-based CLI at ~/.claude/tools/gemini.py using the user's
#   logged-in Chrome cookies. Free under the user's existing Gemini Pro
#   subscription. Slower (~30-60s/turn) and the Chromium window pops up
#   during each call, but no API quota / 429s.
def _provider() -> str:
    p = os.environ.get("PHANTOM_PROVIDER", "").lower()
    if p in ("anthropic", "moonshot", "gemini"):
        return p
    if MODEL.startswith(("kimi-", "moonshot-")):
        return "moonshot"
    if MODEL.startswith("gemini"):
        return "gemini"
    return "anthropic"

PROVIDER = _provider()
API_URL = (
    "https://api.moonshot.ai/v1/chat/completions" if PROVIDER == "moonshot"
    else "https://api.anthropic.com/v1/messages"
)
# Path to the Gemini CLI tool (only used when PROVIDER == "gemini").
GEMINI_CLI = os.path.expanduser(
    os.environ.get("PHANTOM_GEMINI_CLI", "~/.claude/tools/gemini.py")
)

SYSTEM_PROMPT = """You are an autonomous agent driving a {platform_name} computer. The user gives a one-line task; you complete it by issuing screen actions one at a time.

Each turn you receive a fresh screenshot. {coord_instructions}

GROUND IN THE IMAGE: every turn, look at THIS screenshot. Identify what's actually visible — windows, menus, buttons, text — only from the pixels in front of you. Do NOT assume any element is present because the task name or your prior progress mentions it. If you can't see a navigation control in the current image, do not pretend you can; pick a different action that IS supported by what you see.

POST-ACTION SCREENSHOT: this image was taken AFTER your previous action's effect rendered. If you just clicked a toggle, the new state is already on screen. Trust the screenshot — your prior action is done. Don't repeat it.

RE-LOCALIZE EVERY TURN: app layouts shift between contexts. A control at one coordinate on screen A may not exist at all on screen B. NEVER reuse a coordinate from a prior turn — recompute from the current image.

GLOBAL HARD RULES:
▸ NEVER press `escape`. It often closes more than the user wanted (e.g. an entire modal). If a stray menu/popover appeared, click outside it on empty space.
▸ NEVER use `cmd+tab` or other cross-app shortcuts. The runner keeps the target app focused.
▸ NEVER repeat the exact same `(x, y)` you used last turn. If a click missed, the correct target is somewhere else in this image — find it.
▸ NEVER click the macOS menu bar (the strip with the app name at y < 25). It opens system dropdowns.
▸ Only output JSON per the schema below. `click`/`double_click` MUST include both `x` and `y`.

PROGRESS FIELD: every reply MUST include a `progress` string — your one-line running checklist. The runner echoes the most recent value back to you next turn, so it's your only persistent memory. If `progress` says you finished step X, don't redo it. Keep `progress` factual and based on what your action ACTUALLY changed (verifiable from the next screenshot), not what you intended.

TOGGLE-BUTTON RULE (any like / follow / save / subscribe / mute, etc.): a single click flips state; clicking again undoes it. After clicking a toggle, your NEXT action must move on (scroll, key, navigate, done). Use the visible numeric count (e.g. like-count) as proof: read it before clicking, store as `last_count` in progress, compare next turn.
- If the count changed by 1 → toggle landed → MOVE ON. Don't re-click.
- If the count is shown abbreviated (e.g. 1.4万, 13K, 2M) — ±1 changes won't be visible — fall back to the BUTTON LABEL: an "Unfollow"/"Following"/"Liked"/"Subscribed" label means you ARE in that state already.
- After one click on an abbreviated-count toggle, assume it landed and move on; never click 3 turns in a row at similar coords.

Reply with ONLY a JSON object — no prose, no fences. Schema:

{{
  "action": "click" | "double_click" | "type" | "key" | "scroll" | "wait" | "done",
  "x": <num>, "y": <num>,           // for click / double_click
  "text": "<string>",               // for type
  "key": "<combo>",                 // for key, e.g. "{primary_mod}+space", "enter"
  "direction": "up" | "down",       // for scroll
  "reasoning": "<one short sentence on why this single action>",
  "progress": "<running checklist of what's been completed and what's left>"
}}

Action notes:
- ONE action per turn. The next turn shows the result.
- To launch an app on macOS, press `{primary_mod}+space`, `type` the app's native name, then `key: enter`. If the task names an app in a non-Latin script (e.g. Chinese), type it in that script — Spotlight matches the bundled app name.
- IGNORE unrelated windows: terminals, IDEs, log panels, monitor outputs. Don't wait on their spinners or take cues from their text.
- `done` when your `progress` shows the task is fully complete. `wait` when content is still loading."""


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
    # Repair 6: missing `y` key (e.g. `"x": 0.891, "0.102", "reasoning"` →
    # `"x": 0.891, "y": 0.102, "reasoning"`). When the parser sees a bare
    # quoted-or-unquoted numeric value after `"x": <num>,`, infer it's `y`.
    f = re.sub(
        r'("x"\s*:\s*-?[\d.]+\s*,\s*)"?(-?[\d.]+)"?(\s*,)',
        r'\1"y": \2\3', e,
    )
    candidates.append(f)
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
        # Gemini provider uses the user's browser session — no API key needed.
        if PROVIDER != "gemini" and not self.api_key:
            raise VisionError("ANTHROPIC_API_KEY not set")
        if PROVIDER == "gemini" and not os.path.exists(GEMINI_CLI):
            raise VisionError(f"Gemini CLI not found at {GEMINI_CLI}")
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
        # The screencapture is at retina (~2x logical on macOS). Scale is
        # measured against retina, not logical, so PHANTOM_MAX_EDGE >= retina
        # means "send the screenshot as-is" (no downsample). Coord arithmetic
        # still uses logical dims for click dispatch.
        retina_w, retina_h = self.logical_w * 2, self.logical_h * 2
        self.scale_from_retina = min(1.0, _MAX_EDGE / max(retina_w, retina_h))
        self.image_w = max(1, round(retina_w * self.scale_from_retina))
        self.image_h = max(1, round(retina_h * self.scale_from_retina))
        # `self.scale` still reports image-edge / logical-edge (the older
        # variable used by the Anthropic coord-scale-up path).
        self.scale = self.image_w / self.logical_w
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
                "and do NOT include any unit."
            )
        elif PROVIDER == "gemini":
            # Gemini's vision tower returns spatially-grounded answers in
            # normalized 0-1000 ymin,xmin,ymax,xmax tuples by default. For
            # JSON action output, pin to normalized 0-1 fractions like Kimi —
            # the cleanest convention to translate to clicks.
            prompt_w, prompt_h = self.image_w, self.image_h
            coord_instructions = (
                "All x/y coordinates you output are NORMALIZED FRACTIONS in [0, 1]: "
                "(x=0, y=0) is the top-left of the screen, (x=1, y=1) is the bottom-right. "
                "Use up to 3 decimal places (e.g. 0.815). The runner multiplies your fractions "
                "by the OS pixel dimensions automatically — do NOT output raw pixel coordinates "
                "and do NOT include any unit."
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
        self.last_turn: dict | None = None  # populated each next_action() call

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

        # User turn = optional one-line runner feedback + task reminder +
        # last reported progress + new screenshot. Feedback is per-turn,
        # ephemeral, and never carried over — runner regenerates it from
        # the current attempt's outcome alone (e.g. "your last click was
        # dropped because it would have toggled the same button").
        progress_line = (
            f"Your reported progress so far: {self._last_progress!r}\n\n"
            if self._last_progress
            else ""
        )
        feedback_line = f"[Runner feedback] {feedback}\n\n" if feedback else ""
        user_text = (
            f"{feedback_line}Task: {self.task}\n\n{progress_line}"
            "What's the next single action? Return JSON only — and remember to update `progress`."
        )
        if PROVIDER == "gemini":
            # Drive gemini.google.com via the Playwright CLI tool. Each call
            # is a fresh browser session with no carry-over history, so we
            # inline the system prompt every turn. The screenshot is uploaded
            # via --reference; the prompt is passed on argv.
            full_prompt = self.system + "\n\n" + user_text
            try:
                proc = subprocess.run(
                    ["python3", GEMINI_CLI, "ask", full_prompt,
                     "--reference", screenshot_path, "--long"],
                    capture_output=True, timeout=240, check=False,
                )
            except subprocess.TimeoutExpired:
                raise VisionError("gemini CLI timed out (>240s)")
            if proc.returncode != 0:
                err = (proc.stderr or b"")[-400:].decode("utf-8", "ignore")
                raise VisionError(f"gemini CLI rc={proc.returncode}: {err}")
            text = (proc.stdout or b"").decode("utf-8", "ignore").strip()
            self.last_turn = {
                "screenshot": screenshot_path,
                "user_text": user_text,
                "response_text": text,
            }
            dump_path = os.environ.get("PHANTOM_TURN_DUMP")
            if dump_path:
                try:
                    with open(dump_path, "a", encoding="utf-8") as fh:
                        fh.write("\n" + "=" * 80 + "\n")
                        fh.write(f"TURN @ {screenshot_path}\n")
                        fh.write("=" * 80 + "\nSYSTEM:\n" + self.system + "\n")
                        fh.write("-" * 80 + "\nUSER TEXT:\n" + user_text + "\n")
                        fh.write("-" * 80 + "\nRESPONSE:\n" + text + "\n")
                except Exception:
                    pass
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                raise VisionError(f"no JSON in response: {text[:200]}")
            action = _lenient_json_loads(m.group())
            if action is None:
                raise VisionError(f"invalid JSON: {m.group()[:200]}")
            # Gemini gets the same normalized-fraction instruction as Kimi.
            for k, dim in (("x", self.logical_w), ("y", self.logical_h)):
                v = action.get(k)
                if not isinstance(v, (int, float)):
                    continue
                if 0 <= v <= 1.5:
                    action[k] = round(v * dim)
                else:
                    action[k] = max(0, min(dim - 1, round(v)))
            prog = action.get("progress")
            if isinstance(prog, str) and prog.strip():
                self._last_progress = prog.strip()
            # No history accumulation for Gemini (fresh browser each call).
            return action

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
        # Stash the raw turn so the runner/UI can show what was actually sent
        # and what came back. System prompt is the same every turn so we keep
        # it on the client (UI shows it once via a toggle).
        self.last_turn = {
            "screenshot": screenshot_path,
            "user_text": user_text,
            "response_text": text,
        }
        dump_path = os.environ.get("PHANTOM_TURN_DUMP")
        if dump_path:
            try:
                with open(dump_path, "a", encoding="utf-8") as fh:
                    fh.write("\n" + "=" * 80 + "\n")
                    fh.write(f"TURN @ {screenshot_path}\n")
                    fh.write("=" * 80 + "\nSYSTEM:\n" + self.system + "\n")
                    fh.write("-" * 80 + "\nUSER TEXT:\n" + user_text + "\n")
                    fh.write("-" * 80 + "\nRESPONSE:\n" + text + "\n")
            except Exception:
                pass
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
        elif self.scale != 1.0:
            # Map model's image-pixel coords back to logical OS coords.
            # scale = image_edge / logical_edge, so divide to get OS pixels.
            # When scale > 1.0 (retina image, larger than logical), this is
            # a halve. When scale < 1.0 (downsampled), this is an upscale.
            for k in ("x", "y"):
                if isinstance(action.get(k), (int, float)):
                    action[k] = round(action[k] / self.scale)

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
