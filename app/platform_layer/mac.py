"""macOS input/screen via existing phantom-click primitives."""
import os
import sys

# Allow `from phantom import ...` when app is run as `python -m app.main`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import math  # noqa: E402

import Quartz  # noqa: E402

from phantom import PhantomInput, PhantomScreen  # noqa: E402

_KEYMAP = {
    "enter": 36, "return": 36, "tab": 48, "escape": 53, "esc": 53,
    "space": 49, "backspace": 51, "delete": 117,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5,
    "h": 4, "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45,
    "o": 31, "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32,
    "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22,
    "7": 26, "8": 28, "9": 25, "0": 29,
}
_MOD_MAP = {
    "command": 55, "cmd": 55, "meta": 55, "win": 55,
    "shift": 56, "option": 58, "alt": 58, "control": 59, "ctrl": 59,
}


class Input:
    def __init__(self):
        self._pi = PhantomInput()

    def click(self, x: float, y: float):
        self._pi.click(x, y)

    def move_to(self, x: float, y: float):
        """Smooth-move the cursor to (x, y) without clicking. Used by the
        debug 'replay move' button so the user can visually verify the
        AI-returned coords land on the right element."""
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        self._pi._current_x = float(loc.x)
        self._pi._current_y = float(loc.y)
        dist = math.hypot(x - self._pi._current_x, y - self._pi._current_y)
        duration = min(0.8, max(0.15, dist / 1500))
        self._pi.move_to(float(x), float(y), duration=duration)

    def double_click(self, x: float, y: float):
        self._pi.double_click(x, y)

    def drag(self, x1: float, y1: float, x2: float, y2: float):
        self._pi.drag(float(x1), float(y1), float(x2), float(y2))

    def type_text(self, text: str):
        if any(ord(c) > 127 for c in text):
            self._pi.paste_text(text)
        else:
            self._pi.type_text(text)

    def press_combo(self, combo: str):
        parts = [p.strip().lower() for p in combo.split("+")]
        modifiers = [_MOD_MAP[p] for p in parts if p in _MOD_MAP]
        main = next((_KEYMAP[p] for p in parts if p in _KEYMAP), None)
        if main is None:
            raise ValueError(f"unrecognised key combo: {combo!r}")
        self._pi.press_key(main, modifiers=modifiers or None)

    def scroll(self, direction: str = "down", clicks: int = 3,
               x: float | None = None, y: float | None = None):
        """Scroll. CGEventCreateScrollWheelEvent dispatches at the CURRENT
        cursor position, so to scroll inside a specific element (e.g. a modal
        that doesn't accept page-level scroll), move the cursor there first."""
        if x is not None and y is not None:
            self._pi.move_to(x, y)
        dy = -clicks if direction == "down" else clicks
        self._pi.scroll(dy=dy)

    def maximize_foreground_window_if_chrome(self) -> bool:
        """No-op on macOS — the platform's 'maximize' (green button) enters
        fullscreen which HIDES the menu bar and is worse for the agent. The
        runner calls this every turn; we just return False here so the
        cross-platform call site stays clean. See app/platform_layer/win.py
        for the Windows implementation."""
        return False


class Screen:
    def __init__(self):
        self._ps = PhantomScreen()

    def capture(self, output_path: str) -> str:
        return self._ps.capture(output_path)

    def size(self):
        return self._ps.get_screen_size()
