"""Background runner thread: screenshot → vision → action loop."""
import os
import subprocess
import tempfile
import time

from PySide6.QtCore import QThread, Signal

from .platform_layer import Input, Screen
from .vision import VisionClient, VisionError


_FOCUS_APP = os.environ.get("PHANTOM_FOCUS_APP", "")
# After N consecutive same-bucket clicks (all suppressed), the runner forces
# a deterministic escape click. Useful when the model is stuck on a target
# that doesn't actually respond. PHANTOM_ESCAPE_X/Y in OS-logical pixels.
_ESCAPE_AFTER_N_SUPPRESSES = 2
_ESCAPE_X = int(os.environ.get("PHANTOM_ESCAPE_X", "0"))
_ESCAPE_Y = int(os.environ.get("PHANTOM_ESCAPE_Y", "0"))


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

    def cancel(self):
        self._cancel = True

    def _shot_path(self, step: int) -> str:
        return os.path.join(self._tmpdir, f"step_{step:02d}.png")

    def run(self):
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
        consecutive_suppresses = 0
        while True:
            step += 1
            if self._cancel:
                self.failed.emit("cancelled")
                return

            _refocus()
            self.step_started.emit(step, f"Step {step}: capturing screen…")
            shot = self._shot_path(step)
            try:
                screen.capture(shot)
            except Exception as e:
                self.failed.emit(f"screenshot failed: {e}")
                return

            self.step_started.emit(step, f"Step {step}: asking Claude…")
            try:
                action = client.next_action(shot)
            except VisionError as e:
                self.failed.emit(f"vision call failed: {e}")
                return

            if self._cancel:
                self.failed.emit("cancelled")
                return

            new_bucket = _click_bucket(action)
            if new_bucket is not None and new_bucket == last_bucket:
                # Silent suppressor — preserve toggle state. The model gets a
                # fresh screenshot on the next turn and will (hopefully) pick
                # a different action this time.
                self.step_done.emit(step, {
                    "action": "suppressed_repeat_click",
                    "x": action.get("x"), "y": action.get("y"),
                    "reasoning": "runner: same-bucket click as previous turn — dropped to preserve toggle state.",
                })
                consecutive_suppresses += 1
                # Escalation: after N suppressions, run a deterministic
                # escape click (configured via PHANTOM_ESCAPE_X/Y) — usually
                # the user-profile sidebar item — so the agent gets out of
                # whatever loop it was in.
                if (consecutive_suppresses >= _ESCAPE_AFTER_N_SUPPRESSES
                        and _ESCAPE_X and _ESCAPE_Y):
                    inp.click(_ESCAPE_X, _ESCAPE_Y)
                    last_bucket = (round(_ESCAPE_X / _CLICK_BUCKET_PX),
                                   round(_ESCAPE_Y / _CLICK_BUCKET_PX))
                    consecutive_suppresses = 0
                    self.step_done.emit(step, {
                        "action": "forced_escape_click",
                        "x": _ESCAPE_X, "y": _ESCAPE_Y,
                        "reasoning": f"runner: {_ESCAPE_AFTER_N_SUPPRESSES} suppressions in a row — clicked escape coord ({_ESCAPE_X},{_ESCAPE_Y}).",
                    })
                time.sleep(_POST_ACTION_DELAY_S)
                continue
            else:
                consecutive_suppresses = 0

            try:
                self._dispatch(inp, action)
            except Exception as e:
                self.failed.emit(f"action {action.get('action')!r} failed: {e}")
                return

            # Track the click only if it actually dispatched. Non-click actions
            # (key, scroll, wait, done) reset the streak so the next click can
            # land legitimately.
            last_bucket = new_bucket

            self.step_done.emit(step, action)

            if action.get("action") == "done":
                self.finished_ok.emit(action.get("reasoning") or "done")
                return

            # Pause so click effects (animations, modals, like-count updates)
            # fully render before the next screenshot.
            time.sleep(_POST_ACTION_DELAY_S)

    def _dispatch(self, inp: Input, action: dict):
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
            inp.type_text(action.get("text", ""))
        elif act == "key":
            inp.press_combo(action.get("key", ""))
        elif act == "scroll":
            # Move the cursor to where the model wants to scroll, defaulting
            # to slightly right of screen center (covers the typical modal
            # footprint on Douyin desktop). Lets the model pass `scroll_x`/
            # `scroll_y` (in OS-logical pixels) when it knows better.
            sx, sy = action.get("scroll_x"), action.get("scroll_y")
            inp.scroll(action.get("direction", "down"),
                       x=sx if sx is not None else self._scroll_default_x,
                       y=sy if sy is not None else self._scroll_default_y)
        elif act in ("done", "wait"):
            return
        else:
            raise ValueError(f"unknown action {act!r}")
