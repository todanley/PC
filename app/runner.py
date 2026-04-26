"""Background runner thread: screenshot → vision → action loop."""
import os
import tempfile
import time

from PySide6.QtCore import QThread, Signal

from .platform_layer import Input, Screen
from .vision import VisionClient, VisionError


# Wall-clock delay between dispatching an action and capturing the next
# screenshot. Lets click animations / page transitions render before the
# next vision call.
_POST_ACTION_DELAY_S = 1.0


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
                action = client.next_action(shot)
            except VisionError as e:
                self.failed.emit(f"vision call failed: {e}")
                return

            if self._cancel:
                self.failed.emit("cancelled")
                return

            try:
                self._dispatch(inp, action)
            except Exception as e:
                self.failed.emit(f"action {action.get('action')!r} failed: {e}")
                return

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
