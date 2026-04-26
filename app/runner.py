"""Background runner thread: screenshot → vision → action loop."""
import os
import tempfile
import time

from PySide6.QtCore import QThread, Signal

from .platform_layer import Input, Screen
from .vision import VisionClient, VisionError


# Stuck detection. We track repeated identical action signatures across turns.
# Why action-sig only (not perceptual screen hash): on video-feed apps the
# pixels change every frame, so screen-hash equality is False even when the UI
# is actually stuck — the model just keeps clicking the same heart toggle.
# Action repetition is the more reliable signal.
#   2nd identical action in a row → inject FEEDBACK in next prompt.
#   3rd identical action in a row → runner forces a scroll to advance.
_FEEDBACK_AT = 2
_FORCE_SCROLL_AT = 3


def _action_signature(a: dict) -> str:
    """Canonical key for 'is this the same action we just dispatched.'"""
    act = a.get("action")
    if act in ("click", "double_click"):
        # Round to a 24px bucket so 2-3px micro-jitter (1588,526 vs 1588,524)
        # still hashes to the same signature.
        return f"{act}:{round(a.get('x', 0) / 24)}:{round(a.get('y', 0) / 24)}"
    if act == "type":
        return f"type:{a.get('text', '')[:40]}"
    if act == "key":
        return f"key:{a.get('key', '')}"
    if act == "scroll":
        return f"scroll:{a.get('direction', '')}"
    return act or "?"


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

        prev_sig: str | None = None
        sig_streak = 1            # how many turns the current sig has run for
        pending_feedback: str | None = None
        step = 0

        while True:
            step += 1
            if self._cancel:
                self.failed.emit("cancelled")
                return

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

            if self._cancel:
                self.failed.emit("cancelled")
                return

            new_sig = _action_signature(action)
            # `wait`/`done` don't count as repeats — wait is meant to recur on
            # loading screens, and done exits the loop below.
            counts_as_repeat = (
                new_sig == prev_sig
                and prev_sig is not None
                and not new_sig.startswith(("wait", "done"))
            )
            sig_streak = sig_streak + 1 if counts_as_repeat else 1
            prev_sig = new_sig

            # 3rd identical real-action in a row — model is stuck on a toggle
            # button or non-interactive coord. Force a scroll to break out.
            if counts_as_repeat and sig_streak >= _FORCE_SCROLL_AT:
                self.step_done.emit(step, {
                    "action": "force_scroll",
                    "reasoning": f"runner: {sig_streak}rd repeat of {new_sig!r} — scrolling to advance.",
                })
                inp.scroll("down")
                # Reset state so we don't keep firing forced scrolls.
                prev_sig = "scroll:down"
                sig_streak = 1
                pending_feedback = (
                    f"FEEDBACK: You repeated {new_sig} {_FORCE_SCROLL_AT} times with no progress, so I "
                    "scrolled down for you. The screenshot now shows a NEW position. Do NOT "
                    "click the same element again — find the next item to act on."
                )
                time.sleep(0.9)
                continue

            # 2nd identical real-action — warn the model on the next turn.
            if counts_as_repeat and sig_streak >= _FEEDBACK_AT:
                pending_feedback = (
                    f"FEEDBACK: You just chose {new_sig} twice in a row. If the first attempt "
                    "didn't visibly progress the task, the element is probably a toggle "
                    "(clicking unlikes/unfollows it) or non-interactive. MOVE ON: scroll to "
                    "the next item, or pick a different element. Do NOT repeat the same "
                    "coordinates a third time — I will force-scroll if you do."
                )

            try:
                self._dispatch(inp, action)
            except Exception as e:
                self.failed.emit(f"action {action.get('action')!r} failed: {e}")
                return

            self.step_done.emit(step, action)

            if action.get("action") == "done":
                self.finished_ok.emit(action.get("reasoning") or "done")
                return

            # Pause so click effects (page transitions, modals) have time to render.
            time.sleep(0.9)

    def _dispatch(self, inp: Input, action: dict):
        act = action.get("action")
        if act == "click":
            inp.click(action["x"], action["y"])
        elif act == "double_click":
            inp.double_click(action["x"], action["y"])
        elif act == "type":
            inp.type_text(action.get("text", ""))
        elif act == "key":
            inp.press_combo(action.get("key", ""))
        elif act == "scroll":
            inp.scroll(action.get("direction", "down"))
        elif act in ("done", "wait"):
            return
        else:
            raise ValueError(f"unknown action {act!r}")
