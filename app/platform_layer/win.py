"""Windows input/screen via pyautogui + mss."""
import time

import pyautogui
import mss
import mss.tools

pyautogui.FAILSAFE = False  # don't abort if cursor hits a corner mid-task
pyautogui.PAUSE = 0.0       # we manage our own pacing


_MOD_TRANSLATE = {
    "cmd": "ctrl", "command": "ctrl", "meta": "ctrl", "win": "winleft",
    "option": "alt",
}
_KEY_TRANSLATE = {
    "return": "enter", "esc": "escape",
}


class Input:
    def click(self, x: float, y: float):
        pyautogui.moveTo(x, y, duration=0.35)
        pyautogui.click()

    def move_to(self, x: float, y: float):
        pyautogui.moveTo(x, y, duration=0.35)

    def double_click(self, x: float, y: float):
        pyautogui.moveTo(x, y, duration=0.35)
        pyautogui.doubleClick()

    def type_text(self, text: str):
        # pyautogui.typewrite can't handle non-ASCII reliably; fall back to clipboard
        if any(ord(c) > 127 for c in text):
            try:
                import pyperclip
                pyperclip.copy(text)
                time.sleep(0.05)
                pyautogui.hotkey("ctrl", "v")
                return
            except ImportError:
                pass
        pyautogui.typewrite(text, interval=0.05)

    def press_combo(self, combo: str):
        parts = [p.strip().lower() for p in combo.split("+")]
        translated = [_MOD_TRANSLATE.get(p, _KEY_TRANSLATE.get(p, p)) for p in parts]
        pyautogui.hotkey(*translated)

    def drag(self, x1: float, y1: float, x2: float, y2: float):
        """Press at (x1,y1), drag to (x2,y2), release. For slider CAPTCHAs
        and similar gestures. pyautogui.dragTo is the equivalent of macOS's
        bezier-curve drag; it's good enough for the CAPTCHA targets we hit."""
        pyautogui.moveTo(x1, y1, duration=0.2)
        pyautogui.dragTo(x2, y2, duration=0.6, button="left")

    def scroll(self, direction: str = "down", clicks: int = 3,
               x: float | None = None, y: float | None = None):
        """Scroll. pyautogui.scroll dispatches at the CURRENT cursor position,
        so to scroll inside a specific element (e.g. a modal that doesn't
        accept page-level scroll), move the cursor there first — same contract
        as the macOS layer."""
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=0.15)
        pyautogui.scroll(-clicks if direction == "down" else clicks)


class Screen:
    def capture(self, output_path: str) -> str:
        with mss.mss() as sct:
            mon = sct.monitors[1]  # primary monitor
            img = sct.grab(mon)
            mss.tools.to_png(img.rgb, img.size, output=output_path)
        return output_path

    def size(self):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            return (mon["width"], mon["height"])
