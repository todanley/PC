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
        """One-shot setup for browser-targeted tasks. In order:

          1. Ensure Chrome is open and frontmost (launches it if not
             running; restores + focuses an existing window if it is).
             Keyboard / API only — no mouse clicks on the taskbar.
          2. Maximize the foreground window via Win32 ShowWindow.
          3. Reset zoom to 100 % then bump it `zoom_steps`*10 % (default
             130 %) so fonts and icons are larger — the model localizes
             small UI targets (back arrows, follow buttons) much more
             reliably at 130 % than at 100 %.

        Design rule: use keyboard / API for anything that can be reliably
        reached that way; never use mouse clicks for app focus / launch /
        zoom because coordinate-based clicks are fragile (DPI, taskbar
        position, theme).

        Why not Win+Up for maximize: (a) third-party Chinese accelerators
        (UU加速器 etc.) register Win+Up as a global hotkey, (b) under
        pyautogui's rapid hotkey delivery the Win keyUp can race with the
        subsequent `=` press and trigger Windows Magnifier (Win+=). The
        Win32 ShowWindow API sends zero keyboard events and sidesteps both."""
        self._ensure_chrome_focused()

        import ctypes
        SW_MAXIMIZE = 3
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
            time.sleep(0.30)
        except Exception:
            pass

        # Defensive: release any modifier that might still be physically /
        # logically held from earlier input, so the next hotkey can't be
        # misread as a Win+ or Alt+ combo. Cheap belt-and-suspenders.
        for k in ("winleft", "winright", "alt", "altleft", "altright",
                  "shift", "ctrl"):
            try:
                pyautogui.keyUp(k)
            except Exception:
                pass
        time.sleep(0.08)

        # Browser zoom: reset to 100 % then bump. Ctrl+0 / Ctrl+= are
        # accepted by Chrome, Edge, Firefox, and all major Chinese browsers
        # (360, QQ, Sogou). No Win-key involvement, no Magnifier risk.
        pyautogui.hotkey("ctrl", "0")
        time.sleep(0.20)
        for _ in range(max(0, zoom_steps)):
            pyautogui.hotkey("ctrl", "=")
            time.sleep(0.12)

    def _ensure_chrome_focused(self, timeout_s: float = 10.0) -> bool:
        """Make Chrome the foreground window. Launch it via the OS shell if
        not already running; restore + bring to front if it is. Returns
        True iff Chrome ended up focused. Best-effort — failure (Chrome not
        installed, SetForegroundWindow denied, etc.) is non-fatal; the
        prelude proceeds and the maximize/zoom land on whatever IS focused.
        """
        try:
            import win32gui
            import win32con
        except ImportError:
            return False

        def find_chrome() -> int | None:
            """Walk all top-level windows for a visible Chrome one. Chrome
            sets its main window class to 'Chrome_WidgetWin_1'; the title
            ends with ' - Google Chrome' (English) or '- Google Chrome'
            without space in some locales. Match either."""
            found: list[int] = []
            def cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                try:
                    cls = win32gui.GetClassName(hwnd)
                    title = win32gui.GetWindowText(hwnd)
                except Exception:
                    return True
                if cls == "Chrome_WidgetWin_1" and title:
                    found.append(hwnd)
                return True
            try:
                win32gui.EnumWindows(cb, None)
            except Exception:
                return None
            return found[0] if found else None

        hwnd = find_chrome()
        if hwnd is None:
            # Not running — launch via the shell. `start chrome` resolves
            # via the registered HTTP handler / App Paths, so we don't need
            # an absolute path. CREATE_NO_WINDOW (0x08000000) avoids a
            # cmd.exe flash; DETACHED_PROCESS (0x00000008) means we don't
            # block on its exit.
            import subprocess
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", "chrome"],
                    creationflags=0x08000000 | 0x00000008,
                    close_fds=True,
                )
            except Exception:
                return False
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                time.sleep(0.3)
                hwnd = find_chrome()
                if hwnd:
                    break
            if hwnd is None:
                return False

        try:
            # Restore if minimized; then raise to foreground. SetForegroundWindow
            # has Win32-side restrictions (the calling thread must have
            # recently received input). For a runner thread kicked off by a
            # button click that just happened, we're usually inside that
            # window — it works. If it fails the maximize still runs on the
            # currently-focused window, which is at least visible.
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.30)
            return True
        except Exception:
            return False

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
