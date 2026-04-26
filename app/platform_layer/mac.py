"""macOS input/screen via existing phantom-click primitives."""
import os
import sys

# Allow `from phantom import ...` when app is run as `python -m app.main`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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

    def double_click(self, x: float, y: float):
        self._pi.double_click(x, y)

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

    def scroll(self, direction: str = "down", clicks: int = 3):
        dy = -clicks if direction == "down" else clicks
        self._pi.scroll(dy=dy)


class Screen:
    def __init__(self):
        self._ps = PhantomScreen()

    def capture(self, output_path: str) -> str:
        return self._ps.capture(output_path)

    def size(self):
        return self._ps.get_screen_size()
