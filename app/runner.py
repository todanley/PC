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

from . import humanize
from . import som
from .platform_layer import Input, Screen
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
# Tasks that say "all / every / 所有 / 全部" implicitly traverse a list.
# Models often declare `done` after seeing the visible top of the list
# without verifying nothing's hidden below. The runner intercepts those
# `done`s and forces a verifying scroll before accepting (see
# _done_needs_scroll_verify usage below).
_LIST_TRAVERSAL_TASK = re.compile(
    r"(\ball\b|\bevery\b|\beach\b|所有|全部|整个|每[一个个个]?)",
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
            # Park the cursor over the content area before screenshotting so
            # hover-triggered controls (e.g. Douyin's video action rail) render
            # for the model. SKIPPED on the first turn: turn 1 must capture the
            # exact state the user handed us, untouched — the model drives from
            # there (and can move the mouse itself if it needs a hover reveal).
            if step > 1:
                try:
                    inp.move_to(self._scroll_default_x, self._scroll_default_y)
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

    def _verify_done_by_scroll(self, screen: Screen, inp: Input,
                               before_path: str, step: int,
                               last_click_xy: tuple[float, float] | None,
                               sw: int, sh: int) -> bool:
        """Try scrolling at several smart positions to see if the screen
        actually has more content than what's currently visible. Returns
        True iff one of the candidate scrolls moved the screen — meaning
        the model's `done` was premature and there's more to process."""
        verify_path = os.path.join(self._tmpdir, f"verify_done_{step:02d}.png")
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
        _add((sw * 0.50, sh * 0.50))   # geometric center
        _add((sw * 0.50, sh * 0.30))   # upper-center (popovers)
        for cx, cy in candidates:
            try:
                inp.scroll("down", x=cx, y=cy)
            except Exception:
                continue
            time.sleep(_POST_ACTION_DELAY_S)
            try:
                screen.capture(verify_path)
            except Exception:
                continue
            if not _scroll_was_noop(before_path, verify_path):
                return True
        return False

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
            # Number of wheel EVENTS per scroll action; each moves
            # PHANTOM_SCROLL_STEP real detents (see Input.scroll). Default 1 —
            # one detent ≈ half a modal viewport, enough to progress without
            # skipping rows. PHANTOM_SCROLL_CLICKS tunes it.
            try:
                clicks = int(os.environ.get("PHANTOM_SCROLL_CLICKS", "1") or 1)
            except ValueError:
                clicks = 1
            inp.scroll(action.get("direction", "down"), clicks=clicks, x=sx, y=sy)
        elif act in ("done", "wait"):
            return
        else:
            raise ValueError(f"unknown action {act!r}")
