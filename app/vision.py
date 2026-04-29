"""Multi-turn Claude vision: ask 'what's the next step' given screenshot + history."""
import base64
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
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
_JPEG_QUALITY = int(os.environ.get("PHANTOM_JPEG_QUALITY", "75"))

MODEL = os.environ.get("PHANTOM_MODEL", "claude-opus-4-7")
ANTHROPIC_VERSION = "2023-06-01"

# Provider routing — picked from MODEL prefix unless PHANTOM_PROVIDER overrides.
# - anthropic (default for claude-*): https://api.anthropic.com/v1/messages
# - moonshot  (for kimi-* / moonshot-*): https://api.moonshot.ai/v1/chat/completions
#   Kimi K2.5 is OpenAI-compatible, ~25x cheaper than Claude Opus on input,
#   and natively multimodal. Set PHANTOM_MODEL=kimi-k2.5 (or kimi-k2.6) and
#   ANTHROPIC_API_KEY=<your moonshot key> (key env name kept for simplicity).
# - gemini    (for gemini-*, when GEMINI_API_KEY is NOT set): no HTTP API
#   key — drives gemini.google.com via the Playwright-based CLI at
#   ~/.claude/tools/gemini.py using the user's logged-in Chrome cookies.
#   Free under the user's Gemini Pro subscription. Slower (~30-60s/turn)
#   and the Chromium window pops up.
# - google    (for gemini-*, when GEMINI_API_KEY IS set): Google AI Studio
#   REST API via OpenAI-compatible endpoint. ~$0.10/$0.40 per M tokens on
#   gemini-2.5-flash-lite, fast (~3-5s/turn), no Chromium pop-up.
def _provider() -> str:
    p = os.environ.get("PHANTOM_PROVIDER", "").lower()
    if p in ("anthropic", "moonshot", "gemini", "google", "computer_use"):
        return p
    if MODEL.startswith(("kimi-", "moonshot-")):
        return "moonshot"
    if "computer-use" in MODEL:
        # gemini-2.5-computer-use-preview-* requires the native
        # generateContent endpoint with the built-in computer_use tool.
        return "computer_use"
    if MODEL.startswith("gemini"):
        # Default to API if a key is provided, else fall back to the
        # Playwright/subscription path so existing setups keep working.
        if os.environ.get("GEMINI_API_KEY"):
            return "google"
        return "gemini"
    return "anthropic"

PROVIDER = _provider()
API_URL = {
    "moonshot": "https://api.moonshot.ai/v1/chat/completions",
    "google":   "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "computer_use": (f"https://generativelanguage.googleapis.com/v1beta/"
                     f"models/{MODEL}:generateContent"),
}.get(PROVIDER, "https://api.anthropic.com/v1/messages")
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

CLICK VERIFICATION (HARD): the runner also pixel-compares a small region around your click point before vs. after each click. If that region is unchanged, the runner injects a `[Runner feedback] Your click at (x, y) had NO visible effect…` line into the next turn. When you see that feedback, you DID NOT actually trigger anything — DO NOT increment any progress counter, DO NOT pretend it succeeded. Treat the failed click as a missed target and re-localize on the new screenshot at a DIFFERENT coordinate (usually the real button is offset by a few tens of pixels).

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
        # Per-provider key resolution. The legacy ANTHROPIC_API_KEY env name
        # is reused for moonshot/anthropic for backwards-compat with existing
        # launchers; google uses its own GEMINI_API_KEY so both can coexist.
        if PROVIDER in ("google", "computer_use"):
            self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        else:
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if PROVIDER == "gemini":
            if not os.path.exists(GEMINI_CLI):
                raise VisionError(f"Gemini CLI not found at {GEMINI_CLI}")
        elif not self.api_key:
            key_name = ("GEMINI_API_KEY"
                        if PROVIDER in ("google", "computer_use")
                        else "ANTHROPIC_API_KEY")
            raise VisionError(f"{key_name} not set")
        plat, primary_mod = _platform_strings()
        self.logical_w, self.logical_h = screen_size
        self._last_progress: str = ""  # echoed back to the model each turn
        # No downsampling — the screencapture is sent at its native resolution
        # so the model gets every pixel of the UI. Image dimensions equal
        # logical screen dimensions (true on 1x displays; on Retina the
        # screencapture is 2x and this needs revisiting). Coord pipeline:
        # model returns x/y in image-pixel space, runner divides by self.scale
        # (= 1.0 here) to get OS click-space — i.e. pass-through.
        self.image_w = self.logical_w
        self.image_h = self.logical_h
        self.scale = 1.0
        prompt_w, prompt_h = self.image_w, self.image_h
        # Gemini's spatial training uses a 0-1000 normalized grid (per Google's
        # image-understanding docs); fighting that convention with "use pixel
        # coords" instructions degraded localization noticeably. Anthropic /
        # Moonshot localize directly in pixel space. Match the prompt to each
        # family's native convention; the runner converts back to OS pixels.
        if PROVIDER in ("google", "gemini"):
            coord_instructions = (
                "All x/y coordinates you output are NORMALIZED to a 0-1000 "
                "grid: x is 0 at the left edge, 1000 at the right edge; y is "
                "0 at the top, 1000 at the bottom — REGARDLESS of the actual "
                f"image's {prompt_w}x{prompt_h} pixel size. This matches "
                "Gemini's spatial-understanding convention. Output integers "
                "in [0, 1000]. The runner converts them to OS click-space "
                "automatically."
            )
        else:
            coord_instructions = (
                f"All x/y coordinates you output are integer PIXEL positions in "
                f"this {prompt_w}x{prompt_h} image. (0, 0) is the top-left pixel; "
                f"({prompt_w}, {prompt_h}) is the bottom-right. Measure them off "
                "the image directly, the same way you'd describe pixel positions "
                "in any picture. The runner converts them to OS click-space "
                "automatically — you don't need to. Output integers, not fractions."
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

    def _to_logical_xy(self, action: dict) -> None:
        """Map the model's reported x/y into OS-logical click coordinates,
        in-place. Three conventions to handle:
          • Gemini family: 0-1000 normalized → multiply by image_dim/1000.
          • Stray fraction (≤ 1.5) from any provider: treat as 0-1 → multiply
            by logical_dim. Belt-and-suspenders.
          • Anthropic / Moonshot: pixel coords in image space → divide by
            self.scale (= image_w / logical_w) to get logical pixels.
        Result is clamped to [0, dim - 1]."""
        gemini_norm = PROVIDER in ("google", "gemini", "computer_use")
        for k, logical_dim, image_dim in (
            ("x", self.logical_w, self.image_w),
            ("y", self.logical_h, self.image_h),
        ):
            v = action.get(k)
            if not isinstance(v, (int, float)):
                continue
            if 0 <= v <= 1.5:
                px = round(v * logical_dim)
            elif gemini_norm:
                px = round(v / 1000 * image_dim / self.scale)
            else:
                px = round(v / self.scale)
            action[k] = max(0, min(logical_dim - 1, px))

    def _encode_image(self, screenshot_path: str) -> tuple[str, str]:
        """Encode the raw screencapture as JPEG without resizing. Steps down
        JPEG quality if the encoded bytes would exceed the per-image cap, but
        never drops resolution. Returns (base64_data, media_type)."""
        img = Image.open(screenshot_path)
        rgb = img.convert("RGB")
        for quality in (_JPEG_QUALITY, 65, 55, 45):
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= _MAX_BYTES:
                return base64.b64encode(data).decode("ascii"), "image/jpeg"
        raise VisionError(
            f"raw screenshot exceeds {_MAX_BYTES} bytes even at JPEG q=45 — "
            "raise PHANTOM_JPEG_QUALITY threshold or re-introduce downsampling."
        )

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
            #
            # Gemini's web chat is heavily RLHF'd toward conversational
            # output; without an aggressive JSON-only frame it asks
            # follow-up questions instead of emitting the action JSON. The
            # wrapper below: (a) leads with the task in `<task>` tags so
            # Gemini can't claim it doesn't see it, (b) closes with a hard
            # OUTPUT-FORMAT directive that's the LAST thing the model
            # reads (closing instructions dominate in chat models).
            full_prompt = (
                "You are executing a structured action loop. Do NOT ask "
                "follow-up questions. Do NOT engage in conversation. Output "
                "ONE JSON object per the schema below — nothing before or "
                "after it.\n\n"
                "<instructions>\n" + self.system + "\n</instructions>\n\n"
                "<turn>\n" + user_text + "\n</turn>\n\n"
                "OUTPUT FORMAT: respond with EXACTLY ONE JSON object, "
                "nothing else. Your reply must START with `{` and END with "
                "`}`. No prose, no markdown fences, no questions. If "
                "uncertain, output: "
                '{"action":"wait","reasoning":"need more info",'
                '"progress":"<your progress>"}'
            )
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
            self._to_logical_xy(action)
            prog = action.get("progress")
            if isinstance(prog, str) and prog.strip():
                self._last_progress = prog.strip()
            # No history accumulation for Gemini (fresh browser each call).
            return action

        if PROVIDER == "computer_use":
            return self._next_action_computer_use(
                screenshot_path, b64, user_text, feedback
            )

        if PROVIDER in ("moonshot", "google"):
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
            # max_tokens omitted: OpenAI-compat endpoints treat it as optional
            # and fall back to the model's full output ceiling. We previously
            # capped at 4096 as cost control, but thinking-mode models
            # (Gemini 3.x Pro, Kimi K2.5) burn the budget on hidden reasoning
            # tokens and the visible JSON gets cut mid-string. Letting the
            # provider's default kick in trades a slightly fuzzier per-call
            # cost for reliable completion.
            body = {"model": MODEL, "messages": messages}
            if PROVIDER == "moonshot":
                # K2.5/K2.6 default to thinking-mode which puts chain-of-
                # thought into `reasoning_content` and leaves `content`
                # empty until the thinking finishes — and even uncapped, the
                # extra latency hurts. Disable.
                body["thinking"] = {"type": "disabled"}
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
            # Anthropic requires max_tokens (unlike OpenAI-compat) so we keep
            # it, set to the largest value Claude 4.x accepts. Effectively
            # uncapped for our use (action JSONs are < 1KB).
            body = {"model": MODEL, "max_tokens": 32000,
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
        # Retry transient 429s with exponential backoff. Moonshot's
        # `engine_overloaded_error` and Anthropic's overloaded_error both
        # come back as 429 and resolve on their own within seconds-to-tens
        # of seconds; without a retry one bad moment kills the entire run.
        backoffs = (2, 6, 18)
        payload = None
        last_err: VisionError | None = None
        for attempt in range(len(backoffs) + 1):
            try:
                with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                    payload = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                body_bytes = e.read()
                msg = body_bytes[:300].decode("utf-8", "ignore")
                last_err = VisionError(f"http {e.code}: {msg}")
                # Retry on 429 (rate limit / overloaded) and 5xx (server)
                if e.code == 429 or 500 <= e.code < 600:
                    if attempt < len(backoffs):
                        time.sleep(backoffs[attempt])
                        continue
                raise last_err
            except Exception as e:
                last_err = VisionError(str(e))
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt])
                    continue
                raise last_err
        if payload is None:
            raise last_err or VisionError("vision call returned no payload")

        if PROVIDER in ("moonshot", "google"):
            choice0 = payload.get("choices", [{}])[0]
            text = (choice0.get("message", {}).get("content", "") or "").strip()
            # Surface truncation explicitly: when finish_reason=="length" the
            # JSON is cut mid-stream and `_lenient_json_loads` would silently
            # fail with "no JSON in response". Better to point at the cause.
            if choice0.get("finish_reason") == "length" and not text.rstrip().endswith("}"):
                raise VisionError(
                    f"response truncated (finish_reason=length) — provider's "
                    f"per-model output ceiling was reached. Got {len(text)} "
                    f"chars. Partial: {text[:200]}"
                )
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

        self._to_logical_xy(action)

        # Capture the model's running progress for echo on next turn.
        prog = action.get("progress")
        if isinstance(prog, str) and prog.strip():
            self._last_progress = prog.strip()

        # Record into history WITHOUT the image (token cost stays flat across
        # long tasks). OpenAI-style providers prefer plain strings for
        # text-only content.
        action_json = json.dumps(action, ensure_ascii=False)
        if PROVIDER in ("moonshot", "google"):
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

    # ─── Computer Use (Gemini native generateContent + computer_use tool) ───
    _CU_SYSTEM = (
        "You are driving a macOS desktop computer (NOT a web browser). The "
        "screenshot represents the entire screen. Operate ONLY with these "
        "input primitives: click_at, type_text_at, scroll_document, "
        "scroll_at, key_combination, wait_5_seconds, hover_at, long_press_at, "
        "drag_and_drop. NEVER call open_web_browser, navigate, search, "
        "go_back, or go_forward — those are browser-only and are invalid "
        "here. To launch an app on macOS: key_combination 'cmd+space', then "
        "type_text_at the app's native name (Chinese apps in Chinese), then "
        "key_combination 'enter'. Coordinates are normalized to a 0-1000 "
        "grid. After clicking a toggle (follow / like / save / subscribe), "
        "MOVE ON — don't re-click the same control."
    )
    _CU_EXCLUDED_FNS = ("open_web_browser", "navigate", "search",
                        "go_back", "go_forward")

    def _next_action_computer_use(self, screenshot_path: str, b64: str,
                                  user_text: str, feedback: str | None) -> dict:
        """Native Gemini Computer Use call. Returns an action dict in the
        same shape as other providers (so the runner can dispatch it
        unchanged)."""
        contents = [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": user_text},
            ],
        }]
        body = {
            "system_instruction": {"parts": [{"text": self._CU_SYSTEM}]},
            "contents": contents,
            "tools": [{
                "computer_use": {
                    "environment": "ENVIRONMENT_BROWSER",  # only env supported
                    "excluded_predefined_functions": list(self._CU_EXCLUDED_FNS),
                }
            }],
        }
        url = f"{API_URL}?key={self.api_key}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        backoffs = (2, 6, 18)
        last_err: VisionError | None = None
        payload = None
        for attempt in range(len(backoffs) + 1):
            try:
                with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
                    payload = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                msg = e.read()[:300].decode("utf-8", "ignore")
                last_err = VisionError(f"http {e.code}: {msg}")
                if e.code == 429 or 500 <= e.code < 600:
                    if attempt < len(backoffs):
                        time.sleep(backoffs[attempt]); continue
                raise last_err
            except Exception as e:
                last_err = VisionError(str(e))
                if attempt < len(backoffs):
                    time.sleep(backoffs[attempt]); continue
                raise last_err
        if payload is None:
            raise last_err or VisionError("computer_use: no payload")

        # Pluck the first functionCall part. Gemini may interleave text
        # commentary and a function call; we only need the call.
        cands = payload.get("candidates", [])
        if not cands:
            raise VisionError(f"computer_use: no candidates: {payload}")
        parts = cands[0].get("content", {}).get("parts", []) or []
        fc = next((p["functionCall"] for p in parts if "functionCall" in p), None)
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        commentary = "\n".join(t for t in text_parts if t)
        if fc is None:
            # Model returned text only (it asked a question or wandered).
            # Surface what it said so the runner can fail visibly.
            raise VisionError(
                f"computer_use: no functionCall, only text: {commentary[:300]}"
            )

        action = self._cu_fc_to_action(fc, commentary)
        self.last_turn = {
            "screenshot": screenshot_path,
            "user_text": user_text,
            "response_text": json.dumps(
                {"functionCall": fc, "commentary": commentary},
                ensure_ascii=False,
            ),
        }
        dump_path = os.environ.get("PHANTOM_TURN_DUMP")
        if dump_path:
            try:
                with open(dump_path, "a", encoding="utf-8") as fh:
                    fh.write("\n" + "=" * 80 + "\n")
                    fh.write(f"TURN @ {screenshot_path} (computer_use)\n")
                    fh.write("=" * 80 + "\nSYSTEM:\n" + self._CU_SYSTEM + "\n")
                    fh.write("-" * 80 + "\nUSER TEXT:\n" + user_text + "\n")
                    fh.write("-" * 80 + "\nFUNCTION CALL:\n"
                             + json.dumps(fc, ensure_ascii=False) + "\n")
                    if commentary:
                        fh.write("COMMENTARY:\n" + commentary + "\n")
            except Exception:
                pass
        # Click coords come in 0-1000 normalized; reuse the shared mapper.
        self._to_logical_xy(action)
        return action

    def _cu_fc_to_action(self, fc: dict, commentary: str) -> dict:
        """Map a Computer Use functionCall to phantom-click's action dict.
        Unmapped or browser-only functions degrade to `wait` so the loop
        keeps going instead of crashing."""
        name = fc.get("name", "")
        args = fc.get("args", {}) or {}
        reason = (commentary or f"computer_use:{name}")[:200]
        if name == "click_at":
            return {"action": "click", "x": args.get("x"), "y": args.get("y"),
                    "reasoning": reason, "progress": ""}
        if name == "type_text_at":
            # Embed click+type so _dispatch can do both atomically.
            return {"action": "type", "x": args.get("x"), "y": args.get("y"),
                    "text": args.get("text", ""),
                    "reasoning": reason, "progress": ""}
        if name == "key_combination":
            keys = (args.get("keys") or "").lower().replace("command", "cmd")
            return {"action": "key", "key": keys,
                    "reasoning": reason, "progress": ""}
        if name == "scroll_document":
            return {"action": "scroll",
                    "direction": args.get("direction", "down"),
                    "reasoning": reason, "progress": ""}
        if name == "scroll_at":
            return {"action": "scroll",
                    "direction": args.get("direction", "down"),
                    "scroll_x": args.get("x"), "scroll_y": args.get("y"),
                    "reasoning": reason, "progress": ""}
        if name == "wait_5_seconds":
            return {"action": "wait", "reasoning": reason, "progress": ""}
        if name in ("hover_at", "long_press_at"):
            # Best-effort: treat as a click at the same point. Hover doesn't
            # exist in our input layer; long-press differs from click only
            # in duration which we don't model.
            return {"action": "click", "x": args.get("x"), "y": args.get("y"),
                    "reasoning": f"{name} → click({reason})", "progress": ""}
        # drag_and_drop / unknown → no-op so the run continues.
        return {"action": "wait",
                "reasoning": f"unsupported function {name}; skipped",
                "progress": ""}
