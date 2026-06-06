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

# Build-time config takes precedence in CN-ship builds. Operator dev mode
# (env vars set, no baked literals) falls through to PHANTOM_MODEL.
from app.build_config import BRIDGE_TOKEN, BRIDGE_URL, IS_CN_BUILD
from app import wallet

# User-facing (Chinese) messages for the wallet/quota flow. The runner matches
# these prefixes to surface them verbatim and to trigger the token prompt.
NO_TOKEN_MSG = "请输入令牌后再运行（向管理员购买额度）"
QUOTA_MSG = "额度已用完，请输入新的令牌"

# CN-ship build: pin model + provider + token regardless of env. The whole
# point is "downloads and runs" — env vars don't exist in the user's world.
if IS_CN_BUILD:
    MODEL = os.environ.get("PHANTOM_MODEL_OVERRIDE", "gemini-3.1-pro-preview")
else:
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
    # CN-ship builds always route via google → bridge. No env overrides.
    if IS_CN_BUILD:
        return "google"
    p = os.environ.get("PHANTOM_PROVIDER", "").lower()
    if p in ("anthropic", "moonshot", "gemini", "google"):
        return p
    if MODEL.startswith(("kimi-", "moonshot-")):
        return "moonshot"
    if MODEL.startswith("gemini"):
        # Default to API if a key is provided, else fall back to the
        # Playwright/subscription path so existing setups keep working.
        if os.environ.get("GEMINI_API_KEY"):
            return "google"
        return "gemini"
    return "anthropic"

PROVIDER = _provider()
API_URL = {
    # PHANTOM_MOONSHOT_URL env override — needed when the key is from the
    # platform.moonshot.cn console (Chinese platform) instead of the default
    # international .ai. Same /v1/chat/completions schema either way.
    "moonshot": os.environ.get("PHANTOM_MOONSHOT_URL",
                               "https://api.moonshot.ai/v1/chat/completions"),
    "google":   "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}.get(PROVIDER, "https://api.anthropic.com/v1/messages")
# CN-bridge support: in a CN-ship build, point at the baked bridge URL.
# In dev, PHANTOM_API_BASE still works as an env override.
if IS_CN_BUILD:
    API_URL = BRIDGE_URL.rstrip("/") + "/v1beta/openai/chat/completions"
else:
    _API_BASE_OVERRIDE = os.environ.get("PHANTOM_API_BASE", "").strip().rstrip("/")
    if _API_BASE_OVERRIDE and PROVIDER == "google":
        API_URL = _API_BASE_OVERRIDE + "/v1beta/openai/chat/completions"
# Path to the Gemini CLI tool (only used when PROVIDER == "gemini").
GEMINI_CLI = os.path.expanduser(
    os.environ.get("PHANTOM_GEMINI_CLI", "~/.claude/tools/gemini.py")
)

SYSTEM_PROMPT = """You are an autonomous agent driving a {platform_name} computer. The user gives a one-line task; you complete it by issuing screen actions one at a time.

Each turn you receive a fresh screenshot. {coord_instructions}
{som_instructions}
GROUND IN THE IMAGE: every turn, look at THIS screenshot. Identify what's actually visible — windows, menus, buttons, text — only from the pixels in front of you. Do NOT assume any element is present because the task name or your prior progress mentions it. If you can't see a navigation control in the current image, do not pretend you can; pick a different action that IS supported by what you see.

POST-ACTION SCREENSHOT: this image was taken AFTER your previous action's effect rendered. If you just clicked a toggle, the new state is already on screen. Trust the screenshot — your prior action is done. Don't repeat it.

RE-LOCALIZE EVERY TURN: app layouts shift between contexts. A control at one coordinate on screen A may not exist at all on screen B. NEVER reuse a coordinate from a prior turn — recompute from the current image.

GLOBAL HARD RULES:
▸ PREFER THE MOUSE. Default to pointer actions — `click`, `double_click`, `move`, `scroll`, `drag` — for everything you can reach with the cursor. The keyboard is a fallback, not a shortcut: use `type` only to enter text, and `key` only when there is genuinely no pointer equivalent (e.g. submitting a focused field). Do NOT use keyboard shortcuts to navigate, go back, switch views, or trigger UI — find the on-screen control and click it.
▸ TO GO BACK / DISMISS / EXIT FULLSCREEN / CLOSE A MODAL: click a visible affordance — the in-app back arrow (`<` / ‹ / left chevron, usually top-left of the content area), the `×` / close button, or empty space outside the modal. Do NOT press `key: escape` (the runner rejects it) and do NOT reach for any keyboard shortcut to dismiss — locate the control and click it.
▸ NEVER use `cmd+tab` or other cross-app shortcuts. The runner keeps the target app focused.
▸ NEVER repeat the exact same `(x, y)` you used last turn. If a click missed, the correct target is somewhere else in this image — find it.
▸ NEVER click the macOS menu bar (the strip with the app name at y < 25). It opens system dropdowns.
▸ Only output JSON per the schema below. `click`/`double_click` MUST include both `x` and `y`.

PROGRESS FIELD: every reply MUST include a `progress` string — your one-line running checklist. The runner echoes the most recent value back to you next turn, so it's your only persistent memory. If `progress` says you finished step X, don't redo it. Keep `progress` factual and based on what your action ACTUALLY changed (verifiable from the next screenshot), not what you intended.

BATCH / LIST TASKS — IN-RUN DONE-LIST (for any "all / every / 所有 / 全部 …" job over a list — block all males, like every video, unfollow everyone, …): you handle items ONE AT A TIME. The INSTANT you finish one item — its final action is taken (blocked / skipped / liked / unfollowed / …) — set `done_item` to that item's UNIQUE identifier plus the outcome, e.g. "方一 — female, skipped" or "水豚噜噜 — male, blacklisted". The runner remembers every `done_item` you report FOR THIS RUN and lists them all back to you each turn under "✅ ALREADY PROCESSED". This is the cure for losing your place WITHIN THE RUN: lists scroll back to the top, reload, or get reset by a captcha — the in-run done-list does NOT. The list resets on every new run (no cross-run persistence); each run starts fresh from an empty done-list. HARD RULES:
▸ NEVER re-process an item already on the done-list — even if the list jumped back to the top. Recognise it by its identifier (the username/title) and skip it.
▸ Each turn, act on the next item whose identifier is NOT on the done-list. If EVERY visible item is already done, SCROLL to reveal new ones (don't re-open profiles you've handled).
▸ Report `done_item` exactly once per item, on the turn you complete it. Use the same identifier text you can read on screen so you can match it later.
▸ Declare `done` only when every item is on the done-list AND scrolling reveals nothing new.

CAPTCHA / VERIFICATION CHALLENGES (slider puzzle, click-the-shapes, drag-into-shadow, "I'm not a robot", image grid, rotate-to-upright, etc.): when the current screenshot is a verification challenge, you are the ONLY thing that knows what kind of challenge it is and how to solve it — the runner has NO hardcoded captcha solver, no special action, no per-platform recogniser. Read the on-screen instructions ("请完成", "拖动滑块完成拼图", "select all images with X", …) and respond with the generic actions you already have. Common shapes and how to attack them:
  ▸ Slider / drag-into-shape puzzle: identify the puzzle piece (often a coloured shape on the left of the photo) and the matching gap/shadow (a hole of the SAME shape further right). Then emit `{{"action":"drag","x1":<piece-handle x>,"y1":<handle y>,"x2":<gap x>,"y2":<piece y>}}`. The handle to drag is usually a button BELOW the photo on a horizontal track, NOT the piece in the photo itself. If the magenta marks helped — pick the mark on the piece and the mark on the matching gap, then estimate drag coords from their positions. If the puzzle image is still blank/loading, `wait` first.
  ▸ Click-sequence quiz ("依次点击 A、B、C"): localise each named target in order and click them one per turn in the requested sequence. Note progress so you don't lose your place between turns.
  ▸ Image grid ("select all images containing X"): click each matching cell, then click the confirm button.
  ▸ Rotate / orient puzzle: click rotate or drag to bring the object upright, then confirm.
  ▸ Math / read text ("type the characters below"): use `type` after clicking the input field.
DEFAULT RULE: prefer `drag` for sliders, `click` for buttons, `type` for text fields, `wait` if the page hasn't fully rendered. There is NO `slide_captcha` action — solve the puzzle with the same primitives you use everywhere else. If the puzzle keeps rejecting your attempt, ask for a refresh by clicking 刷新 / refresh icon (only ONCE — repeated refreshes burn turns) and try again on the new instance.

CLICK VERIFICATION (HARD): the runner also pixel-compares a small region around your click point before vs. after each click. If that region is unchanged, the runner injects a `[Runner feedback] Your click at (x, y) had NO visible effect…` line into the next turn. When you see that feedback, you DID NOT actually trigger anything — DO NOT increment any progress counter, DO NOT pretend it succeeded. Treat the failed click as a missed target and re-localize on the new screenshot at a DIFFERENT coordinate (usually the real button is offset by a few tens of pixels).

READING SMALL VISUAL TAGS (gender, badges, status pills, verified marks): when the task makes you act DIFFERENTLY based on a small icon or coloured badge on a profile/card (most commonly a GENDER symbol), do NOT classify it from a glance. In your reasoning, first state the EXACT COLOUR and SHAPE you can see in the pixels, THEN map to a meaning. For gender icons on Chinese social apps (douyin / 小红书 / weibo / …) the convention is fixed:
  ▸ PINK / RED Venus glyph (circle with a CROSS hanging BELOW) ⇒ ♀ FEMALE
  ▸ BLUE Mars glyph (circle with an ARROW pointing UPPER-RIGHT) ⇒ ♂ MALE
  ▸ Bio strip shows only 抖音号 / IP属地 / "X岁" with NO coloured circular gender glyph at all ⇒ NO TAG SET. The "X岁" age badge ALONE is NOT a gender symbol — age is genderless.
If you cannot be CERTAIN about BOTH the colour (pink vs blue) AND the shape (cross-below vs arrow-up-right), treat it as NO TAG and SKIP per any "if not set, don't act" rule in the task. Do NOT guess "male" just because the task asks you to block males — a wrong block on the wrong gender is a real harmful action the user has to manually undo. When in doubt, skip.

TOGGLE-BUTTON RULE (any like / follow / save / subscribe / mute, etc.): a single click flips state; clicking again undoes it. After clicking a toggle, your NEXT action must move on (scroll, key, navigate, done). Use the visible numeric count (e.g. like-count) as proof: read it before clicking, store as `last_count` in progress, compare next turn.
- If the count changed by 1 → toggle landed → MOVE ON. Don't re-click.
- If the count is shown abbreviated (e.g. 1.4万, 13K, 2M) — ±1 changes won't be visible — fall back to the BUTTON LABEL: an "Unfollow"/"Following"/"Liked"/"Subscribed" label means you ARE in that state already.
- After one click on an abbreviated-count toggle, assume it landed and move on; never click 3 turns in a row at similar coords.

Reply with ONLY a JSON object — no prose, no fences. Schema:

{{
  "action": "click" | "double_click" | "type" | "key" | "scroll" | "drag" | "wait" | "done",
  "mark": <int>,                    // (SoM mode) id of the magenta-tagged element to act on. REQUIRED for click/double_click/type whenever the target has a tag. If you also emit x/y, the runner USES the mark and DISCARDS your x/y — do not try to override.
  "x": <num>, "y": <num>,           // for click / double_click
  "text": "<string>",               // for type
  "clear_first": <bool>,            // OPTIONAL for type — set true when the focused field already holds stale text (e.g. an address bar that auto-completed, a search box pre-filled with your last query, a URL bar still showing the previous page). The runner sends select-all (Ctrl/Cmd+A) before typing so the new text REPLACES the old instead of appending. Leave false / omit when the field is empty (just opened, just cleared) — a needless select-all on an empty box wastes nothing but isn't harmless when focus is fragile.
  "key": "<combo>",                 // for key, e.g. "{primary_mod}+space", "enter"
  "direction": "up" | "down",       // for scroll
  "scroll_x": <num>, "scroll_y": <num>, // OPTIONAL and usually OMIT. Leave them out for normal page scrolls AND for centered modals/popovers — the runner auto-targets a sensible point near screen-center that lands inside a centered overlay. Only set them when the scrollable area is clearly OFF-center (e.g. a narrow left sidebar or a right-docked panel), and then use the SAME coordinate convention as click x/y.
  "x1": <num>, "y1": <num>, "x2": <num>, "y2": <num>, // for drag: press at (x1,y1), drag to (x2,y2), release. Use for slider CAPTCHAs, range-sliders, drag-and-drop. Same coord convention as click x/y.
  "reasoning": "<one short sentence on why this single action>",
  "progress": "<running checklist of what's been completed and what's left>",
  "done_item": "<unique id — outcome>"  // OPTIONAL — set ONLY on the turn you finish ONE item of a batch/list task (see DONE-LIST). The runner remembers it for the rest of this run and echoes the full list back each turn so you never redo one.
}}

Action notes:
- ONE action per turn. The next turn shows the result.
- To open an app: if its icon is visible (taskbar, dock, desktop, Start/Launchpad), CLICK it — that's the mouse-first path. Only when no icon is reachable, fall back to keyboard launch (macOS: `{primary_mod}+space`, `type` the app's native name — in its own script for non-Latin names — then `key: enter`).{maximize_note}
- IGNORE unrelated windows: terminals, IDEs, log panels, monitor outputs. Don't wait on their spinners or take cues from their text.
- To scroll: emit `scroll` with just a `direction` and normally NO `scroll_x`/`scroll_y`. The runner scrolls at a point near screen-center, which is inside a centered modal/popover as well as the page — so omitting the coords is the reliable default. Set `scroll_x`/`scroll_y` ONLY when the scrollable region is clearly OFF-center (a left sidebar, a right-docked panel), and express them in the SAME convention as click x/y (do NOT switch to raw pixels). Wrong-convention scroll coords land off-screen and the scroll does nothing.
- `done` when your `progress` shows the task is fully complete. `wait` when content is still loading."""


# Injected into the system prompt only when SoM (Set-of-Mark) is active.
# Tells the model marks are MANDATORY (not preferred) — the runner ignores any
# x/y the model also emits when a mark is present.
SOM_INSTRUCTIONS = """
SET-OF-MARK TAGS (MANDATORY when present): every interactive element worth acting on is outlined with a magenta box and a small numbered badge (e.g. a "3" at the box's top-left corner). These tags are the runner's element index.
▸ ALWAYS respond with `"mark": <that number>` for `click`, `double_click`, and `type`. **DO NOT also emit `x`/`y`** — if you send both, the runner USES THE MARK and DISCARDS your x/y (you cannot override a mark with a coordinate). Pixel estimation is unreliable; the mark resolves to the element's exact bounding-box centre.
▸ Read the number off the magenta badge that belongs to the SPECIFIC element you mean (the avatar, the follow button, the back arrow). Don't pick the badge of a neighbouring element. If two marks overlap, pick the SMALLER / more specific one (e.g. the follow "+" badge mark, not the larger avatar mark covering it).
▸ A `mark` works for `click`, `double_click`, and `type` (for `type`, the runner clicks the tagged field first, then types your `text`).
▸ x/y is a FALLBACK ONLY when the exact control you need has truly NO mark on it. If everything you can see is tagged, you must use marks — never substitute coordinates.

"""


def _platform_strings():
    """Display name + primary-modifier strings used to introduce the host to
    the model. Sourced from `platform_layer.capabilities` so the answer is
    consistent with every other platform-conditional decision in the app."""
    from .platform_layer import capabilities
    return (capabilities.name, capabilities.primary_mod)


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
        # CN-ship builds: baked bridge token always wins — no env var dance.
        if IS_CN_BUILD:
            # Bearer = the user's prepaid wallet token. Dev override
            # PHANTOM_BRIDGE_TOKEN (BRIDGE_TOKEN) lets operator runs skip the
            # in-app prompt; an explicit api_key arg wins (tests).
            self.api_key = api_key or wallet.get_token() or BRIDGE_TOKEN
        elif PROVIDER == "google":
            self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        else:
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if PROVIDER == "gemini":
            if not os.path.exists(GEMINI_CLI):
                raise VisionError(f"Gemini CLI not found at {GEMINI_CLI}")
        elif not self.api_key:
            if IS_CN_BUILD:
                # No wallet token entered yet — UI will prompt for one.
                raise VisionError(NO_TOKEN_MSG)
            key_name = "GEMINI_API_KEY" if PROVIDER == "google" else "ANTHROPIC_API_KEY"
            raise VisionError(f"{key_name} not set")
        # Remaining wallet balance (USD) parsed from the bridge's
        # X-Quota-Remaining-Usd response header; None until the first call.
        self.last_remaining_usd: float | None = None
        plat, primary_mod = _platform_strings()
        self.logical_w, self.logical_h = screen_size
        self._last_progress: str = ""  # echoed back to the model each turn
        # In-run done-list for batch/list tasks (block-all, like-all, …). The
        # model reports each finished item via `done_item`; we keep them all
        # in memory and re-inject every turn so the model never redoes an item
        # after the list scrolls back / reloads / a captcha resets it. Reset
        # to empty on every fresh run — no cross-run persistence; each run
        # discovers the list organically.
        self._done_items: list[str] = []
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
        if PROVIDER in ("google", "gemini", "moonshot"):
            coord_instructions = (
                "All x/y coordinates you output are NORMALIZED to a 0-1000 "
                "grid: x is 0 at the left edge, 1000 at the right edge; y is "
                "0 at the top, 1000 at the bottom — REGARDLESS of the actual "
                f"image's {prompt_w}x{prompt_h} pixel size. This matches the "
                "model family's spatial-grounding convention. Output integers "
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
        # SoM tag instructions only when Set-of-Mark is active for this run;
        # otherwise the placeholder collapses to nothing so the prompt is
        # byte-identical to the pre-SoM build.
        from . import som
        self._som_active = som.enabled()
        som_instructions = SOM_INSTRUCTIONS if self._som_active else ""
        # Windows-only: tell the model to maximize a newly-opened window first
        # (Chrome on a non-maximized window leaves taskbar/other-windows pixels
        # visible, which derails SoM detection and steals clicks). macOS apps
        # don't have an equivalent "fill the screen" mode that helps — the green
        # button enters fullscreen which HIDES the menu bar and is worse for the
        # agent — so the rule is omitted there. Leading newline preserves the
        # bullet-list formatting in the source.
        from .platform_layer import capabilities as _caps
        maximize_note = ("\n- AFTER OPENING ANY APP/BROWSER WINDOW (Chrome, "
            "native apps, file managers, …): if the new window does NOT "
            "already fill the screen edge-to-edge, your VERY FIRST follow-up "
            "action MUST be to maximize it — click the title-bar MAXIMIZE "
            "button (the small SQUARE / \"□\" icon between the `_` minimize "
            "and the `×` close, at the very top-right corner of the window). "
            "A non-maximized Chrome leaves the taskbar, desktop, and other "
            "windows visible behind it, which derails subsequent element "
            "detection and can steal your clicks. SKIP this step only when "
            "the window is ALREADY edge-to-edge (the title-bar slot shows two "
            "overlapping squares \"❐\" instead of one — that's the restore "
            "icon, meaning it IS maximized). Do NOT use a keyboard shortcut; "
            "click the button.") if _caps.prompt_window_maximize else ""
        self.system = SYSTEM_PROMPT.format(
            platform_name=plat, width=prompt_w, height=prompt_h,
            primary_mod=primary_mod, coord_instructions=coord_instructions,
            som_instructions=som_instructions, maximize_note=maximize_note,
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

    def _apply_coords(self, action: dict, mark_map: dict | None) -> None:
        """Resolve the action's target into OS-logical click coords, in-place.

        Policy: when the model emits a `mark`, the mark is AUTHORITATIVE — any
        x/y the model also emitted as a hedge is DISCARDED unconditionally,
        whether the mark resolves or not. Models (e.g. Kimi) tend to emit both
        mark + x/y "to be safe"; letting the wrong x/y win silently undoes the
        whole point of SoM. If the mark fails to resolve (stale / invalid tag),
        the action is turned into a soft no-op (x/y cleared) so the next turn
        re-localises with feedback rather than clicking the model's guess.

        Without a `mark`, fall through to the normal pixel-coord conversion."""
        mk = action.get("mark")
        if mk is not None and mark_map:
            # Mark present → ALWAYS strip any hedge x/y first.
            action.pop("x", None)
            action.pop("y", None)
            # Accept "3", "[3]", "mark 3", "element 3", 3, etc.
            m = re.search(r"\d+", str(mk))
            key = m.group() if m else None
            if key and key in mark_map:
                cx, cy = mark_map[key]
                action["x"] = max(0, min(self.logical_w - 1, round(cx / self.scale)))
                action["y"] = max(0, min(self.logical_h - 1, round(cy / self.scale)))
            # else: mark unknown/stale, x/y already stripped — action will
            # soft-no-op in the runner (missing x/y for click) and the model
            # gets a chance to re-pick next turn. Do NOT fall back to x/y.
            return
        self._to_logical_xy(action)

    def _to_logical_xy(self, action: dict) -> None:
        """Map the model's reported x/y into OS-logical click coordinates,
        in-place. Three conventions to handle:
          • Gemini family: 0-1000 normalized → multiply by image_dim/1000.
          • Stray fraction (≤ 1.5) from any provider: treat as 0-1 → multiply
            by logical_dim. Belt-and-suspenders.
          • Anthropic / Moonshot: pixel coords in image space → divide by
            self.scale (= image_w / logical_w) to get logical pixels.
        Result is clamped to [0, dim - 1]."""
        # Both the Gemini family AND Moonshot Kimi K2.5 emit coords in a
        # 0-1000 normalized grid in practice (verified end-to-end with marker
        # overlays — see /tmp/kimi_probe_marked.png). The earlier code only
        # routed Gemini through the normalized branch and passed Kimi
        # through unchanged, which silently mis-placed every Kimi click.
        gemini_norm = PROVIDER in ("google", "gemini", "moonshot")
        _coord_keys = ("x", "y", "x1", "y1", "x2", "y2", "scroll_x", "scroll_y")
        # If ANY coordinate exceeds the 0-1000 grid, the model emitted RAW
        # IMAGE PIXELS for this action (a recurring Gemini slip). Decide it at
        # the action level: a pixel x (e.g. 1459) often pairs with a pixel y
        # that happens to be <1000 (e.g. 227) — splitting conventions per-axis
        # would map x correctly but mis-place y. So if one is pixels, treat all
        # of this action's coords as pixels.
        action_is_pixel = any(
            isinstance(action.get(k), (int, float)) and action[k] > 1000
            for k in _coord_keys
        )
        # x/y for click/type/scroll, plus x1/y1/x2/y2 for drag.
        for k, logical_dim, image_dim in (
            ("x", self.logical_w, self.image_w),
            ("y", self.logical_h, self.image_h),
            ("x1", self.logical_w, self.image_w),
            ("y1", self.logical_h, self.image_h),
            ("x2", self.logical_w, self.image_w),
            ("y2", self.logical_h, self.image_h),
            ("scroll_x", self.logical_w, self.image_w),
            ("scroll_y", self.logical_h, self.image_h),
        ):
            v = action.get(k)
            if not isinstance(v, (int, float)):
                continue
            if 0 <= v <= 1.5 and not action_is_pixel:
                px = round(v * logical_dim)
            elif gemini_norm and not action_is_pixel:
                px = round(v / 1000 * image_dim / self.scale)
            else:
                # Raw image pixels (either a pixel-coordinate provider, or a
                # normalized provider that slipped and emitted pixels — see
                # action_is_pixel above). The old code ran a stray >1000 value
                # through the /1000 branch → e.g. 1459 → ~5600px → clamped to
                # the screen edge → the click missed and the agent looped.
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

    @staticmethod
    def _done_identifier(item: str) -> str:
        """Identifier portion of a done_item string (the part before the first
        em/en/hyphen dash), normalised for matching. 'water — male, blocked'
        → 'water'."""
        for sep in ("—", "–", " - "):
            if sep in item:
                item = item.split(sep, 1)[0]
                break
        return item.strip().lower()

    def _record_done(self, item: str) -> None:
        """Append a finished batch item to the in-run done-list, deduped by
        identifier so a re-reported item (model confirming twice) is ignored."""
        item = item.strip()
        if not item:
            return
        ident = self._done_identifier(item)
        if not ident:
            return
        if any(self._done_identifier(d) == ident for d in self._done_items):
            return
        self._done_items.append(item)
        # Harness-only trace so a review run can watch the done-list grow.
        if os.environ.get("PHANTOM_RUN_DIR"):
            try:
                import sys
                print(f"[done-list] +{item}  (total {len(self._done_items)})",
                      file=sys.stderr, flush=True)
            except Exception:
                pass

    def next_action(self, screenshot_path: str, feedback: str | None = None,
                    mark_map: dict | None = None) -> dict:
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
        marks_line = ""
        if mark_map:
            marks_line = (
                f"This screenshot has {len(mark_map)} interactive elements "
                "outlined in magenta with numbered tags. Use "
                '`"mark": <number>` for every click / double_click / type — '
                "the runner resolves marks to exact element centres and "
                "DISCARDS any `x`/`y` you also emit. Use x/y only when the "
                "target has truly no tag.\n\n"
            )
        done_line = ""
        if self._done_items:
            # Cap the rendered list so the prompt stays bounded on huge runs;
            # the full set is still kept in memory for dedup.
            shown = self._done_items[-150:]
            elided = len(self._done_items) - len(shown)
            head = f"(+{elided} earlier) " if elided else ""
            items = "\n".join(f"  {i}. {d}" for i, d in enumerate(shown, 1))
            done_line = (
                f"✅ ALREADY PROCESSED {head}({len(self._done_items)} item(s)) — "
                "do NOT redo any of these, even if the list reset to the top:\n"
                f"{items}\n\n"
            )
        user_text = (
            f"{feedback_line}{marks_line}Task: {self.task}\n\n{progress_line}{done_line}"
            "What's the next single action? Return JSON only — update `progress`, "
            "and set `done_item` whenever you finish one item of a list task."
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
            self._apply_coords(action, mark_map)
            prog = action.get("progress")
            if isinstance(prog, str) and prog.strip():
                self._last_progress = prog.strip()
            di = action.get("done_item")
            if isinstance(di, str) and di.strip():
                self._record_done(di)
            # No history accumulation for Gemini (fresh browser each call).
            return action

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
            elif PROVIDER == "google" and MODEL.startswith("gemini-3"):
                # Gemini 3 Pro REQUIRES thinking-mode (the API rejects
                # reasoning_effort=none with "Budget 0 is invalid"). "low" is
                # the minimum permitted; even at "low" a turn can take 60-180s.
                body["reasoning_effort"] = "low"
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

        # Cloudflare's Browser Integrity Check blocks the default urllib UA
        # ("Python-urllib/3.x") with HTTP 403 error 1010. Any custom UA passes.
        # We use a versioned phantom-click string so server logs can attribute
        # traffic to the app version.
        headers.setdefault("User-Agent", "phantom-click/0.1.0")
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
                with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
                    # Bridge echoes the wallet's remaining balance (USD) so the
                    # UI can show the user their own quota, never Gemini tokens.
                    rem = resp.headers.get("X-Quota-Remaining-Usd")
                    if rem is not None:
                        try:
                            self.last_remaining_usd = float(rem)
                        except ValueError:
                            pass
                    payload = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                body_bytes = e.read()
                msg = body_bytes[:300].decode("utf-8", "ignore")
                # 402 = wallet token out of quota. Won't recover on retry —
                # surface the friendly Chinese message so the UI prompts for a
                # new token. (Only the bridge returns 402, and only for an
                # exhausted balance.)
                if e.code == 402:
                    self.last_remaining_usd = 0.0
                    raise VisionError(QUOTA_MSG)
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

        self._apply_coords(action, mark_map)

        # Capture the model's running progress for echo on next turn.
        prog = action.get("progress")
        if isinstance(prog, str) and prog.strip():
            self._last_progress = prog.strip()
        # Accumulate the durable batch done-list (re-injected every turn).
        di = action.get("done_item")
        if isinstance(di, str) and di.strip():
            self._record_done(di)

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
