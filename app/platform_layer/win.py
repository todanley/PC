"""Windows input/screen via pyautogui + mss, with behavioral humanization.

Each public Input.* method shapes its OS calls so the resulting DOM event
stream from a target browser (douyin.com etc.) looks human to behavioral
fingerprinting: bezier mouse paths with micro-tremor, log-normal typing
delays with bigram-aware weights and occasional typos, click dwell, scroll
inertia. Tunable via app/humanize.py; disable entirely with PHANTOM_HUMANIZE=0.
"""
import time

import pyautogui
import mss
import mss.tools

from .. import humanize

pyautogui.FAILSAFE = False  # don't abort if cursor hits a corner mid-task
pyautogui.PAUSE = 0.0       # we manage our own pacing


_MOD_TRANSLATE = {
    "cmd": "ctrl", "command": "ctrl", "meta": "ctrl", "win": "winleft",
    "option": "alt",
}
_KEY_TRANSLATE = {
    "return": "enter", "esc": "escape",
}


def _humanized_move(x_to: float, y_to: float):
    """Bezier-curve move from the current cursor position to (x_to, y_to)
    with per-segment dwell + sub-pixel jitter. When humanization is disabled,
    falls back to a single pyautogui.moveTo with linear easing."""
    x_from, y_from = pyautogui.position()
    distance = ((x_to - x_from) ** 2 + (y_to - y_from) ** 2) ** 0.5
    duration = humanize.move_duration_s(distance)
    if not humanize.ENABLED or duration <= 0:
        pyautogui.moveTo(x_to, y_to, duration=0.35)
        return
    for px, py, dt in humanize.bezier_path(x_from, y_from, x_to, y_to, duration):
        pyautogui.moveTo(px, py)
        if dt > 0:
            time.sleep(dt)


class Input:
    def click(self, x: float, y: float):
        ox, oy = humanize.click_offset_px()
        _humanized_move(x + ox, y + oy)
        hover = humanize.pre_click_hover_s()
        if hover > 0:
            time.sleep(hover)
        pyautogui.mouseDown()
        dwell = humanize.click_dwell_s()
        if dwell > 0:
            time.sleep(dwell)
        pyautogui.mouseUp()

    def move_to(self, x: float, y: float):
        _humanized_move(x, y)

    def double_click(self, x: float, y: float):
        ox, oy = humanize.click_offset_px()
        _humanized_move(x + ox, y + oy)
        hover = humanize.pre_click_hover_s()
        if hover > 0:
            time.sleep(hover)
        # Two separate down-up pairs, each with their own dwell, separated
        # by a small inter-click gap (real double-clicks: ~50-120 ms apart).
        for _ in range(2):
            pyautogui.mouseDown()
            dwell = humanize.click_dwell_s()
            if dwell > 0:
                time.sleep(dwell)
            pyautogui.mouseUp()
            time.sleep(0.05 + 0.07 * (humanize.ENABLED and __import__("random").random()))

    def type_text(self, text: str):
        """Type ASCII text key-by-key with log-normal inter-key delays,
        bigram-aware weights, and occasional typo+correction. Non-ASCII
        falls back to clipboard paste (Chinese, etc.) — keystroke-level
        humanization doesn't apply when the whole string lands in one Ctrl+V.
        """
        if not text:
            return
        if any(ord(c) > 127 for c in text):
            try:
                import pyperclip
                pyperclip.copy(text)
                time.sleep(0.05)
                pyautogui.hotkey("ctrl", "v")
                return
            except ImportError:
                pass

        # Decide once whether this run gets a typo.
        do_typo = humanize.should_typo(text)
        typo_i, wrong_char = (-1, "")
        if do_typo:
            typo_i, wrong_char = humanize.pick_typo(text)

        prev = ""
        i = 0
        while i < len(text):
            c = text[i]
            if i == typo_i and wrong_char:
                # Type the wrong char, pause as if noticing, backspace,
                # then drop through to type the correct char.
                pyautogui.typewrite(wrong_char, interval=0)
                time.sleep(humanize.key_delay_s(prev, wrong_char))
                time.sleep(0.15 + 0.20 * humanize.click_dwell_s())  # noticing
                pyautogui.press("backspace")
                time.sleep(0.05 + humanize.key_delay_s(wrong_char, c))
                typo_i = -1  # only one typo per run
            pyautogui.typewrite(c, interval=0)
            if i < len(text) - 1:
                time.sleep(humanize.key_delay_s(c, text[i + 1]))
            prev = c
            i += 1

    def press_combo(self, combo: str):
        # Modifier+key combos are atomic from the user's perspective —
        # humanizing the inter-key timing here doesn't help and risks
        # breaking shortcuts that need precise simultaneity (e.g. Ctrl+V).
        parts = [p.strip().lower() for p in combo.split("+")]
        translated = [_MOD_TRANSLATE.get(p, _KEY_TRANSLATE.get(p, p)) for p in parts]
        pyautogui.hotkey(*translated)

    def drag(self, x1: float, y1: float, x2: float, y2: float):
        """Press at (x1,y1), drag along a bezier path to (x2,y2), release.
        The path uses the same humanization as Input.move_to so slider
        CAPTCHAs (Douyin's puzzle slider, etc.) see a realistic
        hand-driven motion."""
        _humanized_move(x1, y1)
        time.sleep(humanize.click_dwell_s())
        pyautogui.mouseDown(button="left")
        time.sleep(humanize.click_dwell_s())
        distance = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        duration = max(0.3, humanize.move_duration_s(distance) * 1.5)
        if humanize.ENABLED and duration > 0:
            for px, py, dt in humanize.bezier_path(x1, y1, x2, y2, duration):
                pyautogui.moveTo(px, py)
                if dt > 0:
                    time.sleep(dt)
        else:
            pyautogui.moveTo(x2, y2, duration=0.6)
        time.sleep(humanize.click_dwell_s())
        pyautogui.mouseUp(button="left")

    def browser_prelude(self, zoom_steps: int = 3):
        """One-shot setup for browser-targeted tasks: maximize the focused
        window via Win+Up (avoids any mouse drift / titlebar misclick), then
        reset zoom and step it up by `zoom_steps`*10% so fonts and icons are
        larger — the model localizes small UI targets (back arrows, follow
        buttons) much more reliably at 130 % than at 100 %.

        Caller's contract: the browser window has focus before this runs.
        On focused-non-browser windows, Win+Up still maximizes but Ctrl+=
        is harmless in most apps."""
        # Maximize. Win+Up: minimized → restored, normal → maximized,
        # maximized → no-op. Idempotent for our purposes.
        pyautogui.hotkey("winleft", "up")
        time.sleep(0.4)
        # Reset zoom to 100 % so the next steps land on a known starting
        # point regardless of whatever the user had previously.
        pyautogui.hotkey("ctrl", "0")
        time.sleep(0.2)
        # Zoom in. Browsers accept Ctrl+= as the unshifted-key form of
        # Ctrl++; pyautogui's `=` maps to the correct VK on Windows.
        for _ in range(max(0, zoom_steps)):
            pyautogui.hotkey("ctrl", "=")
            time.sleep(0.10)

    def scroll(self, direction: str = "down", clicks: int = 3,
               x: float | None = None, y: float | None = None):
        """Variable-speed scroll: dispatch wheel ticks one at a time with
        randomized inter-tick delays so the resulting `wheel` event stream
        in the browser has natural inertia instead of a single bulk burst."""
        if x is not None and y is not None:
            _humanized_move(x, y)
        sign = -1 if direction == "down" else 1
        for i in range(max(1, clicks)):
            pyautogui.scroll(sign)
            if i < clicks - 1:
                delay = humanize.scroll_step_delay_s()
                if delay > 0:
                    time.sleep(delay)


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
