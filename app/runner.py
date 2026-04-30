"""Background runner thread: screenshot → vision → action loop."""
import os
import re
import shutil
import subprocess
import tempfile
import time

import numpy as np
from PIL import Image, ImageChops
from PySide6.QtCore import QThread, Signal

from .platform_layer import Input, Screen
from .vision import VisionClient, VisionError


def _click_was_noop(before_path: str, after_path: str,
                    cx: float, cy: float,
                    screen_size: tuple[int, int],
                    half_win: int = 90,
                    diff_threshold: float = 4.0) -> bool:
    """Compare a 2*half_win square centered on the click point in the
    before/after screenshots. Return True when the region is essentially
    unchanged — strong signal the click missed its target / hit dead area /
    bounced off a non-clickable element. False when the region clearly
    changed (button toggled, modal opened, etc.).

    Cropping at the click point isolates us from confounding changes
    elsewhere on screen (e.g. Douyin's autoplay advancing the feed even
    when the click does nothing) — an issue that would defeat full-image
    comparison. We measure mean per-pixel absolute RGB delta; threshold
    set empirically (4 / 255 ≈ JPEG / cursor-render noise floor).
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
    diff = ImageChops.difference(a.crop(box), b.crop(box))
    mean = float(np.asarray(diff, dtype=np.uint8).mean())
    return mean < diff_threshold


def _scroll_was_noop(before_path: str, after_path: str,
                     diff_threshold: float = 4.0) -> bool:
    """Full-image pixel-diff before vs. after a scroll. Return True when the
    screen barely changed — a strong signal the wheel event landed on a
    region that doesn't accept it (e.g. screen center while a modal is
    open: the wheel goes to the page underneath, which is dimmed and
    inert).

    Threshold is set above the noise floor we see from background
    animations on apps like Douyin (autoplaying preview thumbnails in the
    feed area produce a mean delta of ~1-3 even when nothing the user
    cares about changed). A real scroll moving ~50 px of list content
    gives a much larger mean delta than that."""
    try:
        a = Image.open(before_path).convert("RGB")
        b = Image.open(after_path).convert("RGB")
    except Exception:
        return False
    if a.size != b.size:
        return False
    diff = ImageChops.difference(a, b)
    mean = float(np.asarray(diff, dtype=np.uint8).mean())
    return mean < diff_threshold


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
# screenshot. Lets click animations / page transitions render before the
# next vision call.
_POST_ACTION_DELAY_S = 1.0
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

    def __init__(self, task: str, parent=None):
        super().__init__(parent)
        self.task = task
        self._cancel = False
        self._tmpdir = tempfile.mkdtemp(prefix="phantom_run_")
        # Default scroll-cursor anchor: 55%/55% of logical screen so the wheel
        # event lands inside a modal-sized area. Filled at run() start once
        # we know screen size.
        self._scroll_default_x = 950
        self._scroll_default_y = 600
        # ffmpeg subprocess that records the entire screen for the duration
        # of the run; lets us replay what actually happened on screen
        # afterwards (cursor moves, click landings, layout shifts) and
        # diagnose mis-localizations. None when recording is disabled or
        # ffmpeg isn't available.
        self._recorder: subprocess.Popen | None = None
        self._recorder_path: str | None = None

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

        step = 0
        last_bucket: tuple[int, int] | None = None
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
        while True:
            step += 1
            if self._cancel:
                self.failed.emit("cancelled")
                return

            _refocus()
            # Park the cursor over the typical content area before
            # screenshotting. Many app UIs (e.g. Douyin's single-video
            # action rail) only render hover-triggered controls when the
            # cursor is over the content; without this they're invisible
            # to the model and it hallucinates coordinates for elements
            # that aren't actually drawn.
            try:
                inp.scroll  # touch attribute to ensure inp is initialized
                inp._pi.move_to(self._scroll_default_x, self._scroll_default_y)  # type: ignore[attr-defined]
                time.sleep(0.25)  # let hover UI paint
            except Exception:
                pass

            self.step_started.emit(step, f"Step {step}: capturing screen…")
            shot = self._shot_path(step)
            try:
                screen.capture(shot)
            except Exception as e:
                self.failed.emit(f"screenshot failed: {e}")
                return

            self.step_started.emit(step, f"Step {step}: asking Claude…")
            try:
                action = client.next_action(shot, feedback=pending_feedback)
            except VisionError as e:
                self.failed.emit(f"vision call failed: {e}")
                return
            pending_feedback = None

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

            # Hard reject menu-bar clicks (y<25). The system prompt warns
            # against this but smaller models (Kimi K2.5, Haiku 4.5) violate
            # it on Step 1 anyway, opening a macOS dropdown that costs the
            # whole next turn to dismiss. Cheap to enforce here.
            if action.get("action") in ("click", "double_click"):
                yv = action.get("y")
                if isinstance(yv, (int, float)) and yv < 25:
                    xv = action.get("x")
                    self.step_done.emit(step, {
                        "action": "rejected_menubar_click",
                        "x": xv, "y": yv,
                        "reasoning": "runner: y<25 hits the macOS menu bar — rejected.",
                    })
                    pending_feedback = (
                        f"Your click at ({xv},{yv}) was REJECTED — y<25 is the "
                        "macOS menu bar and clicking there opens a system dropdown. "
                        "Pick an element INSIDE the app's content area (y≥25). "
                        "Re-localize from THIS screenshot."
                    )
                    time.sleep(_POST_ACTION_DELAY_S)
                    continue

            new_bucket = _click_bucket(action)
            if new_bucket is not None and new_bucket == last_bucket:
                # Silent suppressor — preserve toggle state. The model gets a
                # fresh screenshot on the next turn and a per-turn feedback
                # nudge so it knows the previous attempt was a no-op and
                # localizes a different element from the current screenshot.
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
                self.finished_ok.emit(action.get("reasoning") or "done")
                return

            # Pause so click effects (animations, modals, like-count updates)
            # fully render before the next screenshot.
            time.sleep(_POST_ACTION_DELAY_S)

            # Click-effect verification: when the model just clicked, take
            # a fresh screenshot and compare a region around the click point
            # to the pre-click image. If the region is essentially unchanged
            # we know the click did nothing (missed the target / hit dead
            # space) and feed that back so the model re-localizes instead of
            # blindly incrementing its `progress` counter on a phantom
            # success. Verify image is reused as the next turn's input — no
            # extra screen capture.
            if action.get("action") in ("click", "double_click"):
                cx, cy = action.get("x"), action.get("y")
                if isinstance(cx, (int, float)) and isinstance(cy, (int, float)):
                    verify_path = os.path.join(self._tmpdir,
                                               f"verify_{step:02d}.png")
                    try:
                        screen.capture(verify_path)
                    except Exception:
                        verify_path = None
                    if verify_path and _click_was_noop(
                        shot, verify_path, cx, cy, (sw, sh)
                    ):
                        pending_feedback = (
                            f"Your click at ({cx},{cy}) had NO visible effect — "
                            "the area around that point looks identical before "
                            "and after the click. The element you targeted "
                            "wasn't actually clickable (or you missed it). DO "
                            "NOT increment any progress counter for this "
                            "attempt — the action did not happen. Re-localize "
                            "from the next screenshot and try a DIFFERENT "
                            "coordinate — usually the correct target is a few "
                            "tens of pixels off from where you guessed."
                        )

            # Scroll-effect verification + auto-retry. A scroll that doesn't
            # change the screen almost always means the wheel event went to
            # a region that doesn't accept it (e.g. screen center while a
            # modal is open elsewhere). Before bothering the model with
            # feedback, the runner tries up to a few smart fallback
            # positions — last-click-xy, then right-half-center, then
            # left-half-center. If ANY of those lands a real scroll, the
            # model sees the scrolled screen next turn and never knows the
            # first attempt missed. Only after every fallback fails do we
            # forward feedback so the model can pick a smarter spot.
            if action.get("action") == "scroll":
                verify_path = os.path.join(self._tmpdir,
                                           f"verify_{step:02d}.png")
                try:
                    screen.capture(verify_path)
                except Exception:
                    verify_path = None
                if verify_path and _scroll_was_noop(shot, verify_path):
                    direction = action.get("direction", "down")
                    # Build retry candidates, deduped.
                    candidates: list[tuple[float, float]] = []
                    seen: set[tuple[int, int]] = set()
                    def _add(c):
                        key = (round(c[0]), round(c[1]))
                        if key not in seen:
                            seen.add(key); candidates.append(c)
                    if last_click_xy is not None:
                        _add(last_click_xy)
                    _add((sw * 0.78, sh * 0.50))   # right-half center
                    _add((sw * 0.22, sh * 0.50))   # left-half center
                    _add((sw * 0.50, sh * 0.30))   # upper-center
                    succeeded = False
                    for rx, ry in candidates:
                        try:
                            inp.scroll(direction, x=rx, y=ry)
                        except Exception:
                            continue
                        time.sleep(_POST_ACTION_DELAY_S)
                        try:
                            screen.capture(verify_path)
                        except Exception:
                            continue
                        if not _scroll_was_noop(shot, verify_path):
                            succeeded = True
                            break
                    if not succeeded:
                        used_xy = (action.get("scroll_x") is not None
                                   and action.get("scroll_y") is not None)
                        pending_feedback = (
                            "Your scroll AND the runner's automatic "
                            "fallback retries (last-click point, "
                            "right-half center, left-half center, "
                            "upper-center) all produced NO visible "
                            "screen change. "
                            + ("scroll_x/scroll_y were set but the wheel "
                               "still didn't reach a scrollable element. "
                               if used_xy else
                               "You did not pass scroll_x/scroll_y. ")
                            + "Either (a) the visible list really is at "
                              "its end, or (b) the scrollable element is "
                              "somewhere the runner's heuristics didn't "
                              "guess. If you still believe more content "
                              "exists, RE-ISSUE scroll with scroll_x/y "
                              "pointing at a DIFFERENT spot — pick by "
                              "looking at the screenshot for where the "
                              "scrollbar / list rows live. Otherwise "
                              "treat the list as fully processed."
                        )

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
            # Computer Use's type_text_at carries x,y for the field to focus.
            # Click first to focus, then type. When x/y are absent we just
            # type into whatever already has focus (older provider shape).
            x, y = action.get("x"), action.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                inp.click(x, y)
                time.sleep(0.1)
            inp.type_text(action.get("text", ""))
        elif act == "key":
            inp.press_combo(action.get("key", ""))
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
            inp.scroll(action.get("direction", "down"), x=sx, y=sy)
        elif act in ("done", "wait"):
            return
        else:
            raise ValueError(f"unknown action {act!r}")
