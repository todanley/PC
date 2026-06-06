"""Background runner thread: screenshot → vision → action loop."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image, ImageChops
from PySide6.QtCore import QThread, Signal

from . import humanize
from . import som
from .platform_layer import Input, Screen, capabilities
from .vision import VisionClient, VisionError


def _click_was_noop(before_path: str, after_path: str,
                    cx: float, cy: float,
                    screen_size: tuple[int, int],
                    half_win: int = 100,
                    mean_threshold: float = 2.0,
                    sig_pixel_threshold: int = 30,
                    sig_pixel_delta: int = 25) -> bool:
    """Detect whether a click had any visible effect on screen.

    Two single-signal designs both have failure modes:
      • Full-image mean delta misses small toggle-button flips: a
        ~80×30 px label change against 6.1 M total pixels averages to
        ~0.04 — far below any sane noise threshold — and the runner
        falsely flags successful 已关注/关注 toggles as no-ops.
      • Regional crop with mean-only misses cursor-blink inside an
        empty search box: ~36 changed px in a 200×200 region average
        ~0.1.

    Hybrid: crop a 2*half_win square around the click point (the
    target IS the most likely site of change), then declare the click
    HAD effect if EITHER:
      • mean per-pixel delta in the crop > mean_threshold (catches
        toggle flips, modal opens that animate from the click point,
        large color changes), or
      • count of pixels with any-channel delta > sig_pixel_delta is
        >= sig_pixel_threshold (catches small but distinct visual
        changes like a cursor blink — the cursor's ~36 strong-delta
        pixels easily exceed 30).

    A click is a no-op iff BOTH signals say nothing happened.
    """
    try:
        a = Image.open(before_path).convert("RGB")
        b = Image.open(after_path).convert("RGB")
    except Exception:
        return False
    if a.size != b.size:
        return False
    logical_w, logical_h = screen_size
    sx = a.width / max(1, logical_w)
    sy = a.height / max(1, logical_h)
    px = round(cx * sx)
    py = round(cy * sy)
    box = (max(0, px - half_win), max(0, py - half_win),
           min(a.width, px + half_win), min(a.height, py + half_win))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return False
    diff = np.asarray(ImageChops.difference(a.crop(box), b.crop(box)),
                      dtype=np.uint8)
    mean = float(diff.mean())
    # Per-pixel "did any channel change > delta" → 2-D bool, then sum.
    sig_pixels = int((diff > sig_pixel_delta).any(axis=2).sum())
    return mean < mean_threshold and sig_pixels < sig_pixel_threshold


def _scroll_was_noop(before_path: str, after_path: str,
                     diff_threshold: float = 4.0,
                     region: tuple[float, float, float] | None = None) -> bool:
    """Pixel-diff before vs. after a scroll. Return True when nothing moved —
    a strong signal the wheel landed somewhere inert, or the list is at its end.

    `region=(cx, cy, half)` restricts the comparison to a box around the scroll
    target. This is ESSENTIAL for a small modal/list on a big screen: a real
    multi-row scroll of Douyin's follow modal moves only ~2% of the screen's
    pixels (full-image mean ~2, indistinguishable from the ~1-3 noise floor of
    autoplaying feed thumbnails), yet the modal region itself changes massively
    (region mean ~16 vs 0.0 at the bottom — measured). Cropping to the target
    also excludes that background autoplay noise entirely. Without a region we
    fall back to the full-image mean (correct for full-page scrolls)."""
    try:
        a = Image.open(before_path).convert("RGB")
        b = Image.open(after_path).convert("RGB")
    except Exception:
        return False
    if a.size != b.size:
        return False
    if region is not None:
        cx, cy, half = region
        w, h = a.size
        box = (max(0, int(cx - half)), max(0, int(cy - half)),
               min(w, int(cx + half)), min(h, int(cy + half)))
        if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
            a = a.crop(box); b = b.crop(box)
    diff = ImageChops.difference(a, b)
    mean = float(np.asarray(diff, dtype=np.uint8).mean())
    return mean < diff_threshold




def _frame_sig(path: str):
    """64-bit average-hash of a screenshot, for loop detection: downscale to
    8x8 grayscale, bit = pixel brighter than the frame mean. The Hamming
    distance between two sigs measures how different two screens look. None on
    failure (loop detection is then skipped — never load-bearing)."""
    try:
        a = np.asarray(Image.open(path).convert("L").resize((8, 8)),
                       dtype=np.float32)
    except Exception:
        return None
    m = float(a.mean())
    bits = 0
    for v in a.flatten():
        bits = (bits << 1) | (1 if v > m else 0)
    return bits


def _sig_hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _find_avfoundation_screen_index() -> str | None:
    """Return the avfoundation video-device index for `Capture screen 0`,
    or None if ffmpeg/the device can't be found. Indices vary per machine
    (3 here, often 1-2 elsewhere) so we discover at run time."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, timeout=10,
        )
    except Exception:
        return None
    out = (proc.stderr or b"").decode("utf-8", "ignore")
    for line in out.splitlines():
        m = re.search(r"\[(\d+)\]\s+Capture screen 0", line)
        if m:
            return m.group(1)
    return None


_FOCUS_APP = os.environ.get("PHANTOM_FOCUS_APP", "")


def _refocus():
    """If PHANTOM_FOCUS_APP is set, re-activate that app AND raise every one
    of its windows so none stay buried under another app. Plain `activate`
    only brings the app's menu bar forward; the window can still be hidden
    behind a terminal etc. We iterate AXRaise across all windows because the
    process may have 0, 1, or N — `window 1` errors when N=0 and may not be
    the one we want when N>1."""
    if not _FOCUS_APP:
        return
    script = f'''
        tell application "{_FOCUS_APP}" to activate
        delay 0.05
        tell application "System Events" to tell process "{_FOCUS_APP}"
            try
                set frontmost to true
            end try
            try
                set wins to every window
                repeat with w in wins
                    try
                        set value of attribute "AXMinimized" of w to false
                    end try
                    try
                        perform action "AXRaise" of w
                    end try
                end repeat
            end try
        end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=3)
    except Exception:
        pass


# Wall-clock delay between dispatching an action and capturing the next
# screenshot. Lets click animations / page transitions / lazy-loaded bio
# strips render before the next vision call. The previous adaptive
# UIA-stability mechanism declared "stable" too early on Douyin's profile
# pages (header mounts → UIA "stable" → screenshot fires → gender badge
# paints 1-2 s later → model reads no-tag and skips real males), so we
# replaced it with a flat fixed wait. 3 s covers the slow renders without
# making fast pages feel sluggish. Override with PHANTOM_POST_ACTION_DELAY_S.
_POST_ACTION_DELAY_S = float(os.environ.get("PHANTOM_POST_ACTION_DELAY_S", "3.0"))
# Chrome zoom levels (Ctrl+wheel ticks) to enlarge a slider CAPTCHA so its
# piece/gap/handle are big enough to detect + drag reliably. Each tick is one
# Chrome zoom step (100→110→125→150→175→200…); ~5 ≈ 175-200%.
_CAPTCHA_ZOOM_STEPS = 5
# Loop-breaker: if the same screen recurs many times the agent is stuck (e.g.
# Douyin's video-grid trap — open list → mis-click a video → close → repeat).
# On detection we press Escape (closes the video player / popups) and steer the
# model to re-orient; abort after a few recoveries so a run can't burn its whole
# budget looping. Disable with PHANTOM_LOOPBREAK=0.
_LOOPBREAK_ENABLED = os.environ.get("PHANTOM_LOOPBREAK", "1") != "0"
_LOOPBREAK_WINDOW = 10   # recent frame-sigs remembered
_LOOPBREAK_RECUR = 5     # current frame ~matches >= N of them → looping
_LOOPBREAK_HAM = 6       # avg-hash Hamming distance counted as "same screen"
_LOOPBREAK_ABORT = 4     # give up after this many auto-recoveries
# Tasks that say "all / every / 所有 / 全部" implicitly traverse a list.
# Models often declare `done` after seeing the visible top of the list
# without verifying nothing's hidden below. The runner intercepts those
# `done`s and forces a verifying scroll before accepting (see
# _done_needs_scroll_verify usage below).
_LIST_TRAVERSAL_TASK = re.compile(
    # English: bounded word-match on "all"/"every"/"each".
    # Chinese: 所有 / 全部 / 整个 (already specific), and 每 ONLY when followed
    # by a measure word that turns it into "every <item>" — 每个 / 每条 / 每位
    # / 每名 / 每一个. Bare 每 was matching things like "每关注一个" ("each time
    # you follow one"), causing a false done-veto in capped-count tasks like
    # "follow 10 creators".
    r"(\ball\b|\bevery\b|\beach\b|所有|全部|整个|每(?:一个|[个条位名]))",
    re.IGNORECASE,
)
# Silent dispatch suppressor — see _click_bucket. Two consecutive clicks at
# the same 24-px bucket are almost always the model second-guessing a toggle
# button and would undo the prior click. The runner drops the 2nd dispatch
# silently so toggle state is preserved. The model is told nothing.
_CLICK_BUCKET_PX = 24


def _click_bucket(action: dict) -> tuple[int, int] | None:
    if action.get("action") not in ("click", "double_click"):
        return None
    x, y = action.get("x"), action.get("y")
    if x is None or y is None:
        return None
    return (round(x / _CLICK_BUCKET_PX), round(y / _CLICK_BUCKET_PX))


class TaskRunner(QThread):
    step_started = Signal(int, str)
    step_done = Signal(int, dict)
    # Carries the full vision turn so the UI can show prompt + screenshot +
    # response. Payload: {step, screenshot, user_text, response_text, action,
    # system_prompt} — system_prompt is included only on step 1 (it doesn't
    # change between turns, the UI shows it once via a toggle).
    turn_logged = Signal(dict)
    finished_ok = Signal(str)
    failed = Signal(str)
    # Remaining wallet balance in USD, pushed after each turn so the UI can
    # show the user their quota (bridge/CN builds only).
    quota_updated = Signal(float)

    def __init__(self, task: str, parent=None):
        super().__init__(parent)
        self.task = task
        self._cancel = False
        # PHANTOM_RUN_DIR pins the per-turn artifact directory (screenshots,
        # marked frames, turn dump) to a known, persistent path so a test
        # harness can review what happened. Unset → a throwaway temp dir, as
        # before.
        run_dir = os.environ.get("PHANTOM_RUN_DIR", "").strip()
        if run_dir:
            os.makedirs(run_dir, exist_ok=True)
            self._tmpdir = run_dir
        else:
            self._tmpdir = tempfile.mkdtemp(prefix="phantom_run_")
        # Default scroll-cursor anchor: 55%/55% of logical screen so the wheel
        # event lands inside a modal-sized area. Filled at run() start once
        # we know screen size.
        self._scroll_default_x = 950
        self._scroll_default_y = 600
        # Where the most recent scroll wheel was actually delivered (after UIA
        # snapping). Used to crop the scroll no-op check to the modal/list
        # region — a multi-row scroll of a small modal moves only ~2% of total
        # pixels (full-image mean ~2, below noise) yet changes that region
        # massively (region mean ~16). None until the first scroll.
        self._last_scroll_target: tuple[float, float] | None = None
        # ffmpeg subprocess that records the entire screen for the duration
        # of the run; lets us replay what actually happened on screen
        # afterwards (cursor moves, click landings, layout shifts) and
        # diagnose mis-localizations. None when recording is disabled or
        # ffmpeg isn't available.
        self._recorder: subprocess.Popen | None = None
        self._recorder_path: str | None = None
        # Behavioral pacing: per-task rate limiter + persistent hourly cap.
        # See app/humanize.py for the threat-model rationale.
        self._rate_limiter = humanize.RateLimiter()
        self._task_counter = humanize.TaskCounter()

    def cancel(self):
        self._cancel = True

    def _shot_path(self, step: int) -> str:
        return os.path.join(self._tmpdir, f"step_{step:02d}.png")

    def _start_recorder(self) -> str | None:
        """Start ffmpeg recording the screen to <rundir>/screen.mp4.
        Returns the file path on success, None on any failure (which is
        non-fatal — the run continues without a recording)."""
        if os.environ.get("PHANTOM_RECORD", "1") == "0":
            return None
        idx = _find_avfoundation_screen_index()
        if idx is None:
            return None
        path = os.path.join(self._tmpdir, "screen.mp4")
        try:
            self._recorder = subprocess.Popen(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "avfoundation",
                 "-framerate", "10",
                 "-capture_cursor", "1",
                 "-i", f"{idx}:none",
                 "-vcodec", "libx264",
                 "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p",
                 path],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
        self._recorder_path = path
        return path

    def _stop_recorder(self):
        if not self._recorder:
            return
        # Send 'q' to ffmpeg's stdin so it finalizes the MP4 cleanly. Fall
        # back to terminate/kill if it's unresponsive — better to lose the
        # last second than leave a zombie process.
        try:
            self._recorder.communicate(input=b"q", timeout=8)
        except subprocess.TimeoutExpired:
            try:
                self._recorder.terminate()
                self._recorder.wait(timeout=3)
            except Exception:
                try:
                    self._recorder.kill()
                except Exception:
                    pass
        except Exception:
            pass
        self._recorder = None

    def run(self):
        # Start full-screen recording first so even setup failures get
        # captured — helpful when diagnosing "the app didn't even open".
        rec_path = self._start_recorder()
        if rec_path:
            self.step_started.emit(0, f"📹 Recording → {rec_path}")
        try:
            self._run_inner()
        finally:
            self._stop_recorder()
            if self._recorder_path:
                self.step_started.emit(0, f"📹 Saved → {self._recorder_path}")

    def _run_inner(self):
        # B7: refuse to start if the hourly task cap is reached. Persisted
        # across runs so the user can't bypass it by closing and reopening.
        ok, count = self._task_counter.check_and_record()
        if not ok:
            self.failed.emit(
                f"每小时任务上限已达（{count}/{self._task_counter.max_per_hour}）— "
                "请稍后再试。"
            )
            return

        try:
            screen = Screen()
            inp = Input()
            client = VisionClient(self.task, screen.size())
        except VisionError as e:
            self.failed.emit(f"Vision setup failed: {e}")
            return
        except Exception as e:
            self.failed.emit(f"Setup failed: {e}")
            return

        sw, sh = screen.size()
        self._scroll_default_x = round(sw * 0.55)
        self._scroll_default_y = round(sh * 0.55)

        # Set-of-Mark: when enabled (PHANTOM_SOM=1), each turn's screenshot is
        # annotated with numbered tags on detected interactive elements and
        # the model picks a tag instead of regressing a pixel coordinate. The
        # engine loads its OCR model lazily on first use; if that fails it
        # returns empty maps and we transparently fall back to plain
        # coordinate prompting. Constructing the engine is cheap (no model
        # load yet) so it's safe to do unconditionally behind the flag.
        som_engine = som.get_engine() if som.enabled() else None

        # No setup actions before the task: the run starts from EXACTLY the
        # screen state the user hands us (whatever app/window is open). The
        # very first turn is the model's — it looks at the current screen and
        # decides. We do not open/focus/maximize/zoom any app, and we don't
        # touch the mouse or keyboard before the first screenshot.

        step = 0
        last_action_type: str = "click"
        actions_since_break: int = 0
        last_bucket: tuple[int, int] | None = None
        # True when the most recent dispatched click was detected by the
        # post-action verifier as a no-op. Used to disable the same-bucket
        # suppressor on the very next turn so the model can retry a missed
        # click at a slightly different coord without being silenced. Reset
        # whenever a click DOES change the screen (or any non-click action
        # runs).
        last_click_was_noop = False
        # Counts consecutive turns where the bucket suppressor fired. After
        # `_STUCK_LIMIT` of these in a row, the model is locked into a coord
        # the runner believes is wrong — abort the run with a pointer to the
        # recording so the user / debugger can replay what happened.
        consecutive_stuck = 0
        _STUCK_LIMIT = 3
        pending_feedback: str | None = None
        # Last click coords (logical px) — used as a smart fallback for
        # scrolls when the model didn't pass scroll_x/scroll_y. If the
        # model has been clicking inside a modal/list, the scroll lands
        # in the same region, so the modal actually scrolls instead of
        # the page underneath.
        last_click_xy: tuple[float, float] | None = None
        # PHANTOM_MAX_STEPS bounds a run for testing so it can't loop forever
        # (or rack up irreversible actions) before a human reviews it. 0/unset
        # → no cap, normal production behavior.
        try:
            max_steps = int(os.environ.get("PHANTOM_MAX_STEPS", "0") or 0)
        except ValueError:
            max_steps = 0
        # The same-bucket repeat-click suppressor protects toggle state, but on
        # a list task (e.g. unfollowing down a roster) consecutive legitimate
        # clicks can land in the same bucket after the list re-renders, and the
        # suppressor would silently drop them. PHANTOM_SUPPRESS_REPEAT_CLICKS=0
        # disables it for those runs. Default on (production behavior).
        suppress_repeats = os.environ.get("PHANTOM_SUPPRESS_REPEAT_CLICKS", "1") != "0"
        # When a slider CAPTCHA is on screen, zoom the page in once so the
        # puzzle (piece / gap / slider handle) is big enough to detect and drag
        # reliably — 3 zoom steps make it ~12x larger in area, the difference
        # between the handle estimate landing ON vs. BESIDE the button. Reset
        # zoom when the captcha clears. (Tracked across turns.)
        captcha_zoom_applied = False
        # Entry-zoom: one-shot per run, latched the first time Chrome becomes
        # the OS foreground. We reset zoom to 100 % then bump to ~125 % so UI
        # text and buttons are big enough for the vision model to read on
        # high-DPI displays. Independent of captcha_zoom_applied; the captcha
        # zoom stacks on top temporarily and resets back to this baseline.
        entry_zoom_applied = False
        # Loop-breaker state: recent screen avg-hashes, the step of the last
        # auto-recovery, and a count of CONSECUTIVE rapid recoveries (reset when
        # recoveries are spread out — i.e. real progress happened between).
        recent_sigs: list[int] = []
        consec_breaks = 0
        last_break_step = -99
        while True:
            step += 1
            if max_steps and step > max_steps:
                self.failed.emit(
                    f"reached PHANTOM_MAX_STEPS={max_steps} — stopping (test cap).")
                return
            if self._cancel:
                self.failed.emit("cancelled")
                return

            # B1/B2/B3/B6: humanized inter-action pacing. Skipped on the
            # first iteration (warm-up already covered that). The rate
            # limiter enforces the per-minute cap; inter_action_pause
            # adds an action-type-aware random gap; maybe_subtask_break
            # occasionally inserts a 15-60 s "user got distracted" pause.
            if step > 1:
                self._rate_limiter.wait()
                pause = humanize.inter_action_pause_s(last_action_type)
                if pause > 0:
                    time.sleep(pause)
                actions_since_break += 1
                brk = humanize.maybe_subtask_break_s(actions_since_break)
                if brk > 0:
                    time.sleep(brk)
                    actions_since_break = 0

            _refocus()
            # Park the cursor before screenshotting so hover-triggered controls
            # render. SKIPPED on turn 1 (capture the user's exact state).
            #
            # Default park = content center, which reveals hover-only controls
            # like Douyin's video action rail. BUT if the PREVIOUS action was a
            # click, the cursor is sitting on something the model just opened —
            # moving it to center fires a mouse-leave that CLOSES a
            # hover-dismissed dropdown/menu it just opened. In that case keep
            # the cursor on the last click point so the menu stays up.
            if step > 1:
                park_xy = (self._scroll_default_x, self._scroll_default_y)
                if last_action_type in ("click", "double_click") and last_click_xy is not None:
                    park_xy = last_click_xy
                try:
                    inp.move_to(park_xy[0], park_xy[1])
                    time.sleep(0.25)  # let hover UI paint
                except Exception:
                    pass

            self.step_started.emit(step, f"Step {step}: capturing screen…")
            # Enforce window-maximize on Chrome before the screenshot so the
            # model never sees a windowed Chrome with the taskbar / desktop /
            # other windows peeking through. The SYSTEM_PROMPT bullet asked
            # the model to do this itself, but smaller models (gemini-3.5-
            # flash, claude-haiku, kimi) skip it under load. Doing it
            # runner-side removes the decision: if Chrome is the foreground
            # and not already maximized, we maximize via Win32 ShowWindow.
            # Other foreground windows (Phantom-Click GUI, terminal, IDE) are
            # left alone. On macOS the method is a no-op (the platform's
            # 'maximize' enters fullscreen which is worse for the agent).
            try:
                inp.maximize_foreground_window_if_chrome()
            except Exception:
                pass
            # Entry-zoom: the first time Chrome becomes the foreground in
            # this run, reset its page zoom to default then bump it ~25 %
            # so UI text/buttons are big enough for the vision model to read
            # on high-DPI displays (3840×1600 etc.). At 100 % zoom Chrome's
            # avatars and nav labels render at ~12 px each; after the model-
            # side resize they become unreadable. One-shot per run, gated to
            # Chrome foreground so we don't zoom the Phantom-Click GUI or a
            # terminal. macOS impl is a no-op.
            if not entry_zoom_applied:
                try:
                    if inp.set_chrome_entry_zoom(ticks_above_100=2):
                        entry_zoom_applied = True
                        time.sleep(0.35)  # let zoom transition settle
                except Exception:
                    pass
            shot = self._shot_path(step)
            try:
                screen.capture(shot)
            except Exception as e:
                self.failed.emit(f"screenshot failed: {e}")
                return

            # Set-of-Mark: annotate a COPY of the screenshot with numbered
            # element tags and send that to the model. The unmarked `shot` is
            # kept as the before-image for click/scroll noop verification so
            # the magenta overlays never pollute the pixel diff. mark_map is
            # in image-pixel space (== model coord space); next_action()
            # resolves a returned tag to the element's exact center. On any
            # failure mark_map is empty and we send the original screenshot.
            shot_for_model = shot
            mark_map = None
            if som_engine is not None:
                marked = os.path.join(self._tmpdir, f"mark_{step:02d}.png")
                try:
                    mark_map = som_engine.mark_screenshot(shot, marked)
                except Exception:
                    mark_map = None
                if mark_map:
                    shot_for_model = marked
                    self.step_started.emit(
                        step, f"Step {step}: tagged {len(mark_map)} elements")

            # CAPTCHA zoom: when SoM reports a slider puzzle on screen, zoom the
            # page in ONCE (Ctrl+= ×3) so the puzzle + slider handle are large
            # enough to localize and drag reliably (≈12x area — proven). The
            # next loop iteration re-captures the enlarged puzzle, then the
            # model solves it via slide_captcha. Reset zoom (Ctrl+0) once the
            # captcha clears. Gated by PHANTOM_CAPTCHA_ZOOM (default on).
            if (os.environ.get("PHANTOM_CAPTCHA_ZOOM", "1") != "0"
                    and som_engine is not None and hasattr(inp, "zoom_in")):
                on_captcha = getattr(som_engine, "last_captcha", None) is not None
                if on_captcha and not captcha_zoom_applied:
                    # Ctrl+WHEEL zoom (browser), NOT keyboard Ctrl+= — the
                    # latter can race into Win+= → Windows Magnifier.
                    try:
                        inp.zoom_in(_CAPTCHA_ZOOM_STEPS)
                    except Exception:
                        pass
                    captcha_zoom_applied = True
                    self.step_started.emit(step, f"Step {step}: zoomed in on CAPTCHA")
                    time.sleep(0.6)
                    continue  # re-capture the enlarged puzzle next turn
                if not on_captcha and captcha_zoom_applied:
                    try:
                        inp.zoom_reset(_CAPTCHA_ZOOM_STEPS)
                    except Exception:
                        pass
                    captcha_zoom_applied = False

            # Loop-breaker: if this screen has recurred many times the agent is
            # stuck (e.g. repeatedly mis-clicking the profile video grid into a
            # video player). Press Escape to close any video/popup and steer it
            # to re-orient; abort if it persists. Skipped on captcha screens
            # (Escape would dismiss the puzzle; last_captcha is current here
            # since mark_screenshot already ran).
            if _LOOPBREAK_ENABLED:
                _sig = _frame_sig(shot)
                _on_cap = (som_engine is not None
                           and getattr(som_engine, "last_captcha", None) is not None)
                if _sig is not None and not _on_cap:
                    _recurs = sum(1 for h in recent_sigs
                                  if _sig_hamming(h, _sig) <= _LOOPBREAK_HAM)
                    recent_sigs.append(_sig)
                    if len(recent_sigs) > _LOOPBREAK_WINDOW:
                        recent_sigs.pop(0)
                    if _recurs >= _LOOPBREAK_RECUR:
                        # Count consecutive RAPID recoveries; reset if the last
                        # one was a while ago (the agent recovered + progressed).
                        if step - last_break_step <= 5:
                            consec_breaks += 1
                        else:
                            consec_breaks = 1
                        last_break_step = step
                        if consec_breaks > _LOOPBREAK_ABORT:
                            self.failed.emit(
                                "stuck in a repeating-screen loop (e.g. the "
                                "video-grid trap) — aborting after repeated "
                                "auto-recovery that isn't helping")
                            return
                        try:
                            inp.press_combo("escape")
                            time.sleep(0.4)
                        except Exception:
                            pass
                        recent_sigs.clear()
                        self.step_started.emit(
                            step, f"Step {step}: loop detected → Escape + re-orient")
                        pending_feedback = (
                            "⚠️ STUCK: the same screen has recurred several times "
                            "— you are looping (likely mis-clicking your profile's "
                            "video grid into a video player). I pressed Escape to "
                            "close any open video/popup. Re-orient with a DIFFERENT "
                            "approach than the last action: click 我的 in the left "
                            "sidebar to load your profile at the TOP, then click the "
                            "VISIBLE 关注 count to reopen the following list. Resume "
                            "from your checked list; do NOT repeat what looped.")
                        continue

            self.step_started.emit(step, f"Step {step}: asking Claude…")
            try:
                action = client.next_action(
                    shot_for_model, feedback=pending_feedback,
                    mark_map=mark_map)
            except VisionError as e:
                # Wallet/quota messages are already user-facing Chinese — emit
                # them verbatim (the UI prompts for a token on these). Other
                # vision errors keep the diagnostic prefix.
                m = str(e)
                if m.startswith(("额度", "请输入", "令牌")):
                    self.failed.emit(m)
                else:
                    self.failed.emit(f"vision call failed: {m}")
                return
            pending_feedback = None
            # Push the wallet balance the bridge reported this turn to the UI.
            rem = getattr(client, "last_remaining_usd", None)
            if rem is not None:
                self.quota_updated.emit(float(rem))
            # Remember the action type so the NEXT turn's inter-action pause
            # can match (type/drag get a longer "thinking" prefix, scroll is
            # short, etc.). Set before any continue/return so the next
            # iteration's pacing always has a defined value.
            last_action_type = action.get("action") or last_action_type

            # Surface the raw turn (prompt + screenshot + response) to the UI.
            lt = client.last_turn or {}
            self.turn_logged.emit({
                "step": step,
                "screenshot": lt.get("screenshot", shot),
                "user_text": lt.get("user_text", ""),
                "response_text": lt.get("response_text", ""),
                "action": action,
                # Send system prompt only on step 1 — it's identical every
                # turn and the UI keeps it cached.
                "system_prompt": client.system if step == 1 else None,
            })

            if self._cancel:
                self.failed.emit("cancelled")
                return

            # Hard reject `key: escape`. The system prompt has a NEVER rule
            # against it but Gemini 3 Pro still emits it occasionally to
            # "go back" or dismiss a profile - which on macOS often closes
            # the entire window or kicks the model out of the app entirely.
            # Recovery from that costs many turns. Catch and feed back.
            if action.get("action") == "key":
                k = (action.get("key") or "").lower().strip()
                if k in ("escape", "esc", "key.escape"):
                    self.step_done.emit(step, {
                        "action": "rejected_escape_press",
                        "key": action.get("key"),
                        "reasoning": "runner: 'escape' rejected — closes more than intended.",
                    })
                    pending_feedback = (
                        "Your `key: escape` was REJECTED by the runner. The "
                        "GLOBAL HARD RULES say NEVER press escape — it closes "
                        "modals, dropdowns, or whole windows in unpredictable "
                        "ways and often kicks you out of the target app. To "
                        "go back from a sub-view: look for an in-app back "
                        "arrow (< / ‹ / left chevron, usually top-left of "
                        "content area) and click it. To dismiss a popover: "
                        "click empty space outside it. Re-localize from the "
                        "current screenshot."
                    )
                    time.sleep(_POST_ACTION_DELAY_S)
                    continue

            # slide_captcha: solve a slider jigsaw. The model picked the
            # puzzle piece + the matching gap (marks); we drag the slider
            # HANDLE by the exact piece→gap horizontal distance — precision
            # from image processing, the matching-gap choice from the model.
            # Geometry (handle position) comes from the SoM captcha detection.
            if action.get("action") == "slide_captcha":
                det = getattr(som_engine, "last_captcha", None) if som_engine else None
                pm = re.sub(r"\D", "", str(action.get("piece_mark", "")))
                gm = re.sub(r"\D", "", str(action.get("gap_mark", "")))
                ok = False
                dist = 0.0
                if det and mark_map and pm in mark_map and gm in mark_map:
                    px = mark_map[pm][0]
                    gx = mark_map[gm][0]
                    hx, hy = det["handle"]
                    dist = gx - px
                    if dist > 5:
                        try:
                            inp.drag(hx, hy, hx + dist, hy)
                            ok = True
                        except Exception:
                            ok = False
                self.step_done.emit(step, {
                    "action": "slide_captcha",
                    "reasoning": (f"runner: dragged slider handle by {dist:.0f}px"
                                  if ok else "runner: captcha solve unavailable"),
                })
                if not ok:
                    pending_feedback = (
                        "slide_captcha did not run — the puzzle wasn't detected "
                        "or piece_mark/gap_mark were missing/invalid. If the "
                        "puzzle image is still loading, use `wait`; otherwise "
                        "re-read the magenta marks and pick the PIECE mark plus "
                        "the GAP mark whose shape matches it."
                    )
                last_action_type = "drag"
                time.sleep(_POST_ACTION_DELAY_S)
                continue

            # Hard reject clicks that land in the OS global menu bar (macOS
            # only — `capabilities.menu_bar_max_y` is 0 on Windows so this
            # branch is inert there). The system prompt warns against this
            # but smaller models (Kimi K2.5, Haiku 4.5) violate it on Step 1
            # anyway, opening a macOS dropdown that costs the whole next
            # turn to dismiss. Cheap to enforce here. NOTE: on Windows the
            # title bar, address bar and close button live in the same y
            # band (y<35-ish) and are legitimate targets, so this MUST be
            # gated by platform — a previous regression let the unconditional
            # `y < 25` rule fire on Windows and stuck the agent on Chrome's
            # address bar.
            menu_bar_y = capabilities.menu_bar_max_y
            if menu_bar_y > 0 and action.get("action") in ("click", "double_click"):
                yv = action.get("y")
                if isinstance(yv, (int, float)) and yv < menu_bar_y:
                    xv = action.get("x")
                    self.step_done.emit(step, {
                        "action": "rejected_menubar_click",
                        "x": xv, "y": yv,
                        "reasoning": f"runner: y<{menu_bar_y} hits the OS menu bar — rejected.",
                    })
                    pending_feedback = (
                        f"Your click at ({xv},{yv}) was REJECTED — y<{menu_bar_y} "
                        "is the OS global menu bar and clicking there opens a "
                        "system dropdown. Pick an element INSIDE the app's "
                        f"content area (y≥{menu_bar_y}). Re-localize from THIS "
                        "screenshot."
                    )
                    time.sleep(_POST_ACTION_DELAY_S)
                    continue

            new_bucket = _click_bucket(action)
            # Suppress same-bucket repeats ONLY when the prior click actually
            # had an effect — the goal is preserving the toggle state of a
            # button that successfully toggled. If the previous click was a
            # detected no-op (post-action verification flagged it), the
            # current attempt is a RETRY of a missed click, not a toggle
            # undo, so we let it through.
            if (suppress_repeats and new_bucket is not None and new_bucket == last_bucket
                    and not last_click_was_noop):
                self.step_done.emit(step, {
                    "action": "suppressed_repeat_click",
                    "x": action.get("x"), "y": action.get("y"),
                    "reasoning": "runner: same-bucket click as previous turn — dropped to preserve toggle state.",
                })
                consecutive_stuck += 1
                if consecutive_stuck >= _STUCK_LIMIT:
                    px, py = action.get("x"), action.get("y")
                    rec = self._recorder_path or "(no recording)"
                    self.failed.emit(
                        f"stuck: model emitted same-bucket click ({px},{py}) "
                        f"{consecutive_stuck} turns in a row. "
                        f"Run dir: {self._tmpdir}  Video: {rec}"
                    )
                    return
                px, py = action.get("x"), action.get("y")
                mk = action.get("mark")
                if mk is not None:
                    # SoM mode: the model is choosing tags, not coordinates, so
                    # telling it "don't reuse (x,y)" is meaningless — it'll just
                    # re-pick the same tag (this is exactly how it got stuck in
                    # field testing). Name the dead tag and forbid it.
                    pending_feedback = (
                        f"Clicking mark [{mk}] changed nothing — that tag is NOT "
                        "the control you intended (it may be a label, a decorative "
                        "box, or a non-clickable region). Do NOT pick mark "
                        f"[{mk}] again. Look at the screenshot and choose a "
                        "DIFFERENT tag — the real target (e.g. the 关注/following "
                        "COUNT in a profile header) may be a small number you "
                        "have to read carefully. If no tag sits on it, give x/y "
                        "on that exact spot instead."
                    )
                else:
                    pending_feedback = (
                        f"your previous click at ({px},{py}) didn't change the screen "
                        "(likely missed the target or the element wasn't actually clickable). "
                        "Re-localize from the current screenshot — the layout may have shifted "
                        "since your last attempt — and pick a DIFFERENT coordinate or element. "
                        "Don't repeat the same coordinate."
                    )
                time.sleep(_POST_ACTION_DELAY_S)
                continue
            # Click landed (or non-click action) — reset the stuck counter.
            consecutive_stuck = 0

            try:
                self._dispatch(inp, action, scroll_fallback=last_click_xy)
            except Exception as e:
                self.failed.emit(f"action {action.get('action')!r} failed: {e}")
                return

            # Track the click only if it actually dispatched. Non-click actions
            # (key, scroll, wait, done) reset the streak so the next click can
            # land legitimately.
            last_bucket = new_bucket

            # Remember last click point for the smart scroll fallback. We
            # update on click/double_click only — scrolls inherit the prior
            # click's anchor so they hit the same modal/list region.
            if action.get("action") in ("click", "double_click"):
                cx, cy = action.get("x"), action.get("y")
                if isinstance(cx, (int, float)) and isinstance(cy, (int, float)):
                    last_click_xy = (cx, cy)

            self.step_done.emit(step, action)

            if action.get("action") == "done":
                # Done-veto for list-traversal tasks: if the user asked to
                # process "all/every/所有/全部 …", the model often declares
                # done after seeing the visible top of a list without
                # verifying nothing's hidden below the viewport. Force a
                # scroll at smart fallback positions; if any of them moves
                # the screen, there IS more content the model never saw —
                # reject `done`, inject feedback, continue the loop.
                if _LIST_TRAVERSAL_TASK.search(self.task):
                    revealed = self._verify_done_by_scroll(
                        screen, inp, shot, step, last_click_xy, sw, sh
                    )
                    if revealed:
                        pending_feedback = (
                            "Your `done` was REJECTED. The runner "
                            "verified by scrolling and the screen DID "
                            "change — there is more content below the "
                            "visible window that you did NOT process. "
                            "Re-localize on the next screenshot, finish "
                            "the newly-revealed items, then only declare "
                            "`done` after a verifying scroll truly "
                            "reveals nothing new."
                        )
                        time.sleep(_POST_ACTION_DELAY_S)
                        continue
                self.finished_ok.emit(action.get("reasoning") or "done")
                return

            # Click-effect verification AND adaptive wait-for-load: when the
            # model just clicked, wait ADAPTIVELY (not a fixed 1s) until the
            # screen stops changing — handles slow profile loads where the
            # gender badge / button labels take 1-3s to render. The capture
            # doubles as next turn's input, so a premature one made the agent
            # see a half-loaded page (e.g. Charles classed "no tag, skipped"
            # in the blacklist run because the male ♂ hadn't rendered yet).
            # For non-click actions (scroll/key/type) keep the fixed pause —
            # their effect is usually quick and stable.
            # Flat wait for every action: lets click animations / page
            # transitions / lazy-loaded bio strips render before the next
            # vision call. Click and double_click ALSO save the post-wait
            # capture as the noop-check 'after' image; other actions just
            # sleep and let the next loop iteration take its own shot.
            time.sleep(_POST_ACTION_DELAY_S)
            if (action.get("action") in ("click", "double_click")
                and isinstance(action.get("x"), (int, float))
                and isinstance(action.get("y"), (int, float))):
                verify_path = os.path.join(self._tmpdir,
                                           f"verify_{step:02d}.png")
                try:
                    screen.capture(verify_path)
                except Exception:
                    verify_path = None

            # Inner block below handles the noop check + retries when verify
            # exists; preserved structure so the existing logic re-uses
            # `verify_path`.
            if action.get("action") in ("click", "double_click"):
                cx, cy = action.get("x"), action.get("y")
                if isinstance(cx, (int, float)) and isinstance(cy, (int, float)):
                    # verify_path is already set by the adaptive wait above
                    # (may be None on capture failure).
                    pass
                    if verify_path and _click_was_noop(
                        shot, verify_path, cx, cy, (sw, sh)
                    ):
                        # Auto-retry at small offsets first. The model's
                        # 0-1000 normalized coord has ~4 px precision per
                        # unit on a 3840-wide screen, so small targets like
                        # `<` back arrows (~30 px) often get missed by 10-25
                        # px even when the model is "mostly right". A short
                        # fan-out of nearby points usually rescues the click
                        # without involving the model.
                        rescued = False
                        for ox, oy in (
                            ( 18,   0), (-18,   0),
                            (  0, -18), (  0,  18),
                            ( 14,  14), (-14,  14),
                            ( 14, -14), (-14, -14),
                        ):
                            rx = max(0, min(sw - 1, cx + ox))
                            ry = max(0, min(sh - 1, cy + oy))
                            try:
                                if action.get("action") == "double_click":
                                    inp.double_click(rx, ry)
                                else:
                                    inp.click(rx, ry)
                            except Exception:
                                continue
                            time.sleep(_POST_ACTION_DELAY_S)
                            try:
                                screen.capture(verify_path)
                            except Exception:
                                continue
                            if not _click_was_noop(
                                shot, verify_path, cx, cy, (sw, sh)
                            ):
                                rescued = True
                                # Treat the rescue offset as the effective
                                # click target for the smart-scroll fallback.
                                last_click_xy = (rx, ry)
                                break
                        if rescued:
                            last_click_was_noop = False
                        else:
                            last_click_was_noop = True
                            mk = action.get("mark")
                            mark_note = (
                                f"(You selected mark [{mk}]; it is NOT on a working "
                                f"control — do NOT pick mark [{mk}] again.) "
                                if mk is not None else ""
                            )
                            pending_feedback = (
                                f"{mark_note}Your click at ({cx},{cy}) produced no visible "
                                "change in the area around that point, AND the "
                                "runner's auto-retry at 8 nearby offsets "
                                "(±14-18 px) all also failed. Two cases:\n"
                                "  (a) Target was a TEXT INPUT (search box, "
                                "address bar, comment field, etc.). Empty inputs "
                                "only show a blinking cursor when focused, which "
                                "this detector can miss. If that was your intent, "
                                "your NEXT action should be `type` with the text "
                                "to enter — do NOT re-click a different spot.\n"
                                "  (b) You truly missed by more than ~25 px or "
                                "the element wasn't clickable. Re-localize from "
                                "the next screenshot and try a meaningfully "
                                "different coordinate.\n"
                                "Pick (a) or (b) based on what you were trying "
                                "to click. Do not increment progress for the "
                                "click itself if it failed."
                            )
                    else:
                        last_click_was_noop = False
            else:
                # Non-click action — clear the noop flag so a future click
                # gets the bucket-suppressor protection back.
                last_click_was_noop = False

            # Scroll-effect verification. A scroll that doesn't change the
            # screen usually means the wheel event went to a region that
            # doesn't accept it. We DETECT that (one verify capture, no cursor
            # movement) and tell the model to re-aim its own scroll. We do NOT
            # auto-retry at other points — the old version re-scrolled across
            # right-half / left-half / upper-center, which jerked the mouse
            # around the screen after every missed scroll and looked erratic.
            if action.get("action") == "scroll":
                verify_path = os.path.join(self._tmpdir,
                                           f"verify_{step:02d}.png")
                try:
                    screen.capture(verify_path)
                except Exception:
                    verify_path = None
                scroll_region = (
                    (self._last_scroll_target[0], self._last_scroll_target[1], 450)
                    if self._last_scroll_target is not None else None)
                if verify_path and _scroll_was_noop(shot, verify_path,
                                                    region=scroll_region):
                    # Multi-probe retry: the wheel may have landed somewhere
                    # inert (e.g. the page document behind a centred modal —
                    # whose own scrollable child Chrome didn't expose as a
                    # Scroll-pattern node, so find_scroll_target's UIA pass
                    # missed it). Try 2 alternate positions before giving up
                    # and reporting back. Each probe runs at the SAME wheel
                    # magnitude as the model's original scroll; the moment
                    # one moves pixels, we stop and use that result instead.
                    try:
                        retry_clicks = int(os.environ.get("PHANTOM_SCROLL_CLICKS", "5") or 5)
                    except ValueError:
                        retry_clicks = 5
                    direction = action.get("direction", "down")
                    # Build distinct retry positions. The model's original
                    # target sits at self._last_scroll_target; we probe at
                    # right-of-centre (where a centred modal's scrollbar
                    # typically lives) and at the modal-area centre.
                    seen_positions: set[tuple[int, int]] = set()
                    if self._last_scroll_target is not None:
                        seen_positions.add(
                            (int(self._last_scroll_target[0]) // 8,
                             int(self._last_scroll_target[1]) // 8))
                    retry_targets = [
                        (sw * 0.72, sh * 0.55),  # right-of-centre, scrollbar area
                        (sw * 0.50, sh * 0.55),  # centre fallback
                    ]
                    moved = False
                    for rx, ry in retry_targets:
                        key = (int(rx) // 8, int(ry) // 8)
                        if key in seen_positions:
                            continue
                        seen_positions.add(key)
                        try:
                            inp.scroll(direction, clicks=retry_clicks, x=rx, y=ry)
                        except Exception:
                            continue
                        time.sleep(_POST_ACTION_DELAY_S)
                        retry_path = os.path.join(
                            self._tmpdir,
                            f"verify_{step:02d}_retry.png")
                        try:
                            screen.capture(retry_path)
                        except Exception:
                            continue
                        if not _scroll_was_noop(shot, retry_path,
                                                region=(rx, ry, 450)):
                            moved = True
                            self._last_scroll_target = (rx, ry)
                            break
                        if not _scroll_was_noop(shot, retry_path,
                                                region=(sw * 0.5, sh * 0.5, 600)):
                            moved = True
                            self._last_scroll_target = (rx, ry)
                            break
                    if not moved:
                        # Report the no-op verbatim, no inference about cause.
                        # The model decides what it means in context — could
                        # be end of list, could be wrong scroll region, could
                        # be a modal we couldn't target. Don't push it toward
                        # one conclusion.
                        pending_feedback = (
                            "Your scroll produced NO visible screen change "
                            "after the runner also retried at two alternate "
                            "wheel positions. The pixels under the wheel did "
                            "not move. Decide what this means in context: it "
                            "could indicate the list is at its end, or that "
                            "the scrollable region is somewhere you have not "
                            "targeted yet. If you believe more content exists, "
                            "RE-ISSUE scroll with scroll_x/scroll_y pointed at "
                            "a clearly different scrollable region."
                        )

    def _scroll_target(self, hint_xy: tuple[float, float] | None):
        """Snap a scroll hint onto the center of a UIA element that genuinely
        exposes a vertical Scroll pattern, so the wheel lands on scrollable
        content instead of the model's drifting guess. Returns (x,y) in
        screenshot px, or None to keep the caller's own target.

        Windows-only and best-effort: returns None on platforms without UIA,
        when the UIA source can't run, or when nothing scrollable is found.
        Disable with PHANTOM_SCROLL_UIA=0."""
        if not capabilities.has_uia:
            return None
        if os.environ.get("PHANTOM_SCROLL_UIA", "1") != "1":
            return None
        try:
            from . import uia_win
            return uia_win.find_scroll_target(hint_xy)
        except Exception:
            return None

    def _verify_done_by_scroll(self, screen: Screen, inp: Input,
                               before_path: str, step: int,
                               last_click_xy: tuple[float, float] | None,
                               sw: int, sh: int) -> bool:
        """Scroll ONCE at a single sensible point to check whether a `done` is
        premature (more content below). Returns True iff the screen moved.

        Single point on purpose: the old version cycled the cursor through
        4-5 positions, which looked like the mouse darting around the screen.
        We anchor on the last click (where the list/modal is) or fall back to
        screen-centre — one move, not a sweep."""
        verify_path = os.path.join(self._tmpdir, f"verify_done_{step:02d}.png")
        # Anchor the verify-scroll on the LIST itself, not the last click — the
        # last click is often the 关注/count button in the PAGE HEADER, and
        # scrolling there no-ops and FALSELY accepts a premature `done` (seen on
        # resume: agent re-opens the modal, scrolls once, declares done). The
        # list we're verifying is a centered modal, so snap from screen-centre;
        # fall back to the last click only if no scrollable list is found there.
        tgt = self._scroll_target((sw * 0.5, sh * 0.55))
        if tgt is None and last_click_xy is not None:
            tgt = self._scroll_target(last_click_xy)
        cx, cy = tgt if tgt is not None else (sw * 0.5, sh * 0.55)
        # Burst of events (not one) + region-cropped check, mirroring the live
        # scroll path — a single event moves a modal too little to register, so
        # a one-event full-image probe would falsely accept a premature `done`.
        try:
            probe_clicks = int(os.environ.get("PHANTOM_SCROLL_CLICKS", "5") or 5)
        except ValueError:
            probe_clicks = 5
        try:
            inp.scroll("down", clicks=probe_clicks, x=cx, y=cy)
        except Exception:
            return False
        time.sleep(_POST_ACTION_DELAY_S)
        try:
            screen.capture(verify_path)
        except Exception:
            return False
        return not _scroll_was_noop(before_path, verify_path,
                                    region=(cx, cy, 450))

    def _dispatch(self, inp: Input, action: dict,
                  scroll_fallback: tuple[float, float] | None = None):
        act = action.get("action")
        if act == "click":
            x, y = action.get("x"), action.get("y")
            if x is None or y is None:
                # Kimi K2.5 occasionally drops fields. Treat as a soft no-op
                # so the next turn can recover instead of crashing the run.
                return
            inp.click(x, y)
        elif act == "double_click":
            x, y = action.get("x"), action.get("y")
            if x is None or y is None:
                return
            inp.double_click(x, y)
        elif act == "type":
            # Re-establish focus before typing. Two paths:
            #  1. Action carries x/y (e.g. computer_use's type_text_at) →
            #     click that exact target.
            #  2. No x/y → re-click the last click coord. The model usually
            #     spends one prior turn clicking the input field, then this
            #     turn types — but Gemini Pro can take 30+ s to think
            #     between turns and the input field often loses focus to
            #     the browser idle / hover-out by then. Re-clicking the
            #     same spot just before typing is a cheap way to make sure
            #     keystrokes land in the intended field.
            x, y = action.get("x"), action.get("y")
            if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
                if scroll_fallback is not None:
                    x, y = scroll_fallback
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                inp.click(x, y)
                time.sleep(0.15)
            # Optional clear-first: when the model sets `"clear_first": true`,
            # select all in the focused field and let the type overwrite it.
            # Cures the address-bar concatenation loop where a retry typed
            # `douyin.com` into a field that still held the previous URL,
            # producing `chrome://newtab/douyin.com` and a failed navigation.
            # The model decides per-turn — for empty fields it omits the
            # flag so we don't waste an action on a select-all that
            # consumes the focus.
            if action.get("clear_first"):
                try:
                    inp.press_combo(capabilities.select_all_combo)
                    time.sleep(0.08)
                except Exception:
                    pass
            inp.type_text(action.get("text", ""))
        elif act == "key":
            inp.press_combo(action.get("key", ""))
        elif act == "drag":
            x1, y1 = action.get("x1"), action.get("y1")
            x2, y2 = action.get("x2"), action.get("y2")
            if not all(isinstance(v, (int, float)) for v in (x1, y1, x2, y2)):
                return  # malformed drag — soft no-op so model can recover
            inp.drag(x1, y1, x2, y2)
        elif act == "scroll":
            # scroll target precedence:
            #   1. model-provided scroll_x/scroll_y (it knows where to scroll)
            #   2. scroll_fallback (typically the last click point — if the
            #      model has been clicking inside a modal/list, this routes
            #      the wheel into that same region instead of page-center)
            #   3. (sw*0.55, sh*0.55) — generic page anchor, only useful
            #      when nothing else has happened yet
            sx = action.get("scroll_x")
            sy = action.get("scroll_y")
            if sx is None or sy is None:
                if scroll_fallback is not None:
                    sx, sy = scroll_fallback
                else:
                    sx, sy = self._scroll_default_x, self._scroll_default_y
            # Deterministic targeting: the wheel only scrolls the element under
            # the cursor, and (sx,sy) is just a guess that often drifts onto a
            # non-scrolling zone (a modal's header/padding, or the dimmed page
            # backdrop) → the wheel no-ops and the list never advances. Snap
            # onto a UIA element that ACTUALLY exposes a vertical Scroll
            # pattern, using (sx,sy) only as a hint for WHICH scrollable when
            # several exist. One stable point (the container center) — this is
            # the safe replacement for the old erratic multi-point retry.
            tgt = self._scroll_target((sx, sy))
            if tgt is not None:
                # Harness-only trace (PHANTOM_RUN_DIR is set by the test
                # harness) so a review run can confirm the snap fired and see
                # where the wheel actually went vs. the model's guess.
                if os.environ.get("PHANTOM_RUN_DIR"):
                    print(f"[scroll] UIA snap ({sx},{sy}) -> {tgt}",
                          file=sys.stderr, flush=True)
                sx, sy = tgt
            # Remember where the wheel goes so the no-op check can crop to it.
            self._last_scroll_target = (sx, sy)
            # Number of wheel EVENTS per scroll action. Default 5: web modals
            # like Douyin's follow roster scroll a FIXED small step per wheel
            # event regardless of delta magnitude (~½ a row/event, measured),
            # so a single event moves too little to register or make progress.
            # Five events ≈ 2-3 rows — clear movement with row overlap, and on
            # an ordinary page still just a normal scroll. PHANTOM_SCROLL_CLICKS
            # overrides.
            try:
                clicks = int(os.environ.get("PHANTOM_SCROLL_CLICKS", "5") or 5)
            except ValueError:
                clicks = 5
            inp.scroll(action.get("direction", "down"), clicks=clicks, x=sx, y=sy)
        elif act in ("done", "wait"):
            return
        else:
            raise ValueError(f"unknown action {act!r}")
