"""Windows input/screen via pyautogui + mss, with behavioral humanization.

Each public Input.* method shapes its OS calls so the resulting DOM event
stream from a target browser (douyin.com etc.) looks human to behavioral
fingerprinting: bezier mouse paths with micro-tremor, log-normal typing
delays with bigram-aware weights and occasional typos, click dwell, scroll
inertia. Tunable via app/humanize.py; disable entirely with PHANTOM_HUMANIZE=0.
"""
import os
import time

import pyautogui
import mss
import mss.tools

from .. import humanize

pyautogui.FAILSAFE = False  # don't abort if cursor hits a corner mid-task
pyautogui.PAUSE = 0.0       # we manage our own pacing


# ── Unicode keystroke injection (for non-ASCII text, e.g. Chinese) ──────────
# pyautogui can't type non-ASCII, so the old path copied to the clipboard and
# pressed Ctrl+V — which on some setups opened the Windows Clipboard-History
# popup and never pasted, eating Chinese comments. Win32 SendInput with
# KEYEVENTF_UNICODE injects each character's codepoint directly as a keystroke
# (WM_CHAR) into the focused control — no clipboard, no IME, no Ctrl+V.
import ctypes
from ctypes import wintypes

_ULONG_PTR = wintypes.WPARAM  # pointer-sized integer for dwExtraInfo


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR))


class _MOUSEINPUT(ctypes.Structure):  # only here to size the union correctly
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR))


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUT_UNION))


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004


def _type_unicode(text: str) -> None:
    """Inject `text` as Unicode keystrokes into the focused control. Raises on
    any Win32 failure so the caller can fall back."""
    send = ctypes.windll.user32.SendInput
    for ch in text:
        code = ord(ch)
        # BMP chars go in one event; chars above U+FFFF need a surrogate pair.
        units = [code] if code <= 0xFFFF else [
            0xD800 + ((code - 0x10000) >> 10),
            0xDC00 + ((code - 0x10000) & 0x3FF),
        ]
        for unit in units:
            for flags in (_KEYEVENTF_UNICODE,
                          _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                ki = _KEYBDINPUT(0, unit, flags, 0, 0)
                inp = _INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=ki))
                n = send(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
                if n != 1:
                    raise OSError("SendInput failed")
        time.sleep(0.012)  # small per-char gap so the field keeps up


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
            # Primary: inject Unicode codepoints directly (no clipboard / IME /
            # Ctrl+V). Falls back to clipboard paste only if SendInput fails.
            try:
                _type_unicode(text)
                return
            except Exception:
                pass
            try:
                import pyperclip
                pyperclip.copy(text)
                time.sleep(0.12)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.05)
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

    def _ctrl_key(self) -> str:
        """Return the pyautogui key name that produces a REAL left-Ctrl on this
        machine, detected once and cached.

        Some setups swap Ctrl<->Win at the scancode level (observed on this
        operator's machine — pyautogui 'ctrl' actually presses LWIN). That
        turns every Ctrl shortcut into a Win shortcut: Ctrl+= → Win+= → the
        Windows MAGNIFIER, a lone Ctrl tap → the Start menu, Ctrl+V → Win+V
        clipboard history. We probe the live key state: send 'ctrl' and check
        whether VK_CONTROL actually went down; if not, the machine is swapped
        and 'winleft' yields the real Ctrl. The 'ctrl' probe tap opens the
        Start menu on a swapped machine, so we dismiss it with Escape."""
        cached = getattr(self, "_ctrl_key_cache", None)
        if cached:
            return cached
        # Explicit operator override (set PHANTOM_CTRL_KEY=winleft on a
        # Ctrl<->Win-swapped machine to skip probing entirely).
        override = os.environ.get("PHANTOM_CTRL_KEY", "").strip()
        if override:
            self._ctrl_key_cache = override
            return override
        key = "ctrl"
        try:
            import win32api
            def ctrl_down():
                return bool(win32api.GetAsyncKeyState(0x11) & 0x8000)
            pyautogui.keyDown("ctrl")
            time.sleep(0.04)
            is_ctrl = ctrl_down()
            if not is_ctrl:
                # 'ctrl' is mapped to Win on this machine. Guard the release
                # with an Alt tap so the Win press is NOT a lone tap (which
                # would pop the Start menu); the menu opens only on a solo Win.
                pyautogui.keyDown("alt")
                time.sleep(0.02)
                pyautogui.keyUp("alt")
            pyautogui.keyUp("ctrl")
            time.sleep(0.04)
            if not is_ctrl:
                # Confirm 'winleft' yields a real Ctrl (a harmless Ctrl tap).
                pyautogui.keyDown("winleft")
                time.sleep(0.04)
                if ctrl_down():
                    key = "winleft"
                pyautogui.keyUp("winleft")
                time.sleep(0.04)
        except Exception:
            key = "ctrl"
        self._ctrl_key_cache = key
        return key

    def _wheel_zoom(self, steps: int, direction: int):
        """Browser zoom via Ctrl + mouse-wheel. NEVER use keyboard Ctrl+= for
        zoom: pyautogui's hotkey delivery can let the `=` land while Windows
        still treats a Win key as held, firing Win+= → the Windows MAGNIFIER.
        Ctrl+wheel over the page only scales page content within Chrome — no
        `=` keystroke, no Magnifier. Uses _ctrl_key() so it sends a REAL Ctrl
        even on Ctrl<->Win-swapped machines. Wheel delta is ±120 per step
        (one WHEEL_DELTA = one Chrome zoom level; a raw ±1 is too small to
        register). `direction` is +1 (in) / -1 (out). Cursor is parked over
        the page center first since Ctrl+wheel zoom applies where it lands."""
        ck = self._ctrl_key()
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                cx, cy = mon["width"] // 2, mon["height"] // 2
            pyautogui.moveTo(cx, cy, duration=0)
            time.sleep(0.05)
            pyautogui.keyDown(ck)
            time.sleep(0.05)
            for _ in range(max(0, steps)):
                pyautogui.scroll(direction * 120)
                time.sleep(0.12)
        finally:
            try:
                pyautogui.keyUp(ck)
            except Exception:
                pass

    def zoom_in(self, steps: int = 5):
        """Zoom the page IN by `steps` Chrome zoom levels (Ctrl+wheel)."""
        self._wheel_zoom(steps, 1)

    def zoom_reset(self, steps: int = 5):
        """Zoom the page back OUT by `steps` levels (Ctrl+wheel), undoing an
        equal zoom_in. Wheel-based so it never risks the Magnifier."""
        self._wheel_zoom(steps, -1)

    def drag(self, x1: float, y1: float, x2: float, y2: float):
        """Press at (x1,y1), drag along a human-like eased path to (x2,y2),
        release.

        The motion — acceleration/deceleration (smoothstep), small per-step
        jitter, a brief overshoot-then-settle, and dwells around the
        press/release — is generated HERE and is INDEPENDENT of
        PHANTOM_HUMANIZE. Slider CAPTCHAs (Douyin's puzzle slider, etc.)
        run behavioral analysis on the drag and REJECT robotic motion
        (straight line, constant velocity, no dwell). With humanization
        globally disabled the old path fell back to a single linear
        `moveTo`, which the puzzle silently refused — so for `drag` a
        realistic curve is a correctness requirement, not just anti-bot
        pacing, and we always trace one."""
        import random
        _humanized_move(x1, y1)
        time.sleep(0.12)
        pyautogui.mouseDown(button="left")
        time.sleep(0.16)
        dx, dy = x2 - x1, y2 - y1
        dist = (dx * dx + dy * dy) ** 0.5
        steps = max(25, int(dist / 10))
        # A hand slightly overshoots a fast slider throw, then settles back.
        overshoot = min(12.0, dist * 0.04) if dist > 50 else 0.0
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)            # smoothstep: accel → decel
            ox = overshoot * ease if i > steps - 4 else 0.0
            pyautogui.moveTo(x1 + dx * ease + ox,
                             y1 + dy * ease + random.uniform(-1.0, 1.0))
            time.sleep(random.uniform(0.006, 0.020))
        if overshoot:
            for _ in range(4):                    # settle onto the target
                pyautogui.moveTo(x2 + random.uniform(-1.2, 1.2),
                                 y2 + random.uniform(-1.2, 1.2))
                time.sleep(random.uniform(0.02, 0.05))
        pyautogui.moveTo(x2, y2)
        time.sleep(0.14)
        pyautogui.mouseUp(button="left")

    def _clear_os_overlays(self):
        """Close any stray OS overlay that grabbed focus before the task —
        most often the Win11 Start menu, which sits centered on screen and
        SWALLOWS clicks/scrolls aimed at the browser underneath it (observed
        repeatedly in field runs, where it ate wheel events meant for a modal
        list). Escape closes the Start menu, system search, and most popups,
        and is a harmless no-op on a clean desktop. Two presses spaced apart
        cover a popup that re-arms. The runner bars the *model* from escape,
        but pressing it here at task start (no app modal exists yet) is safe."""
        for _ in range(2):
            try:
                pyautogui.press("escape")
            except Exception:
                pass
            time.sleep(0.12)

    def browser_prelude(self, zoom_steps: int = 3):
        """One-shot setup for browser-targeted tasks. In order:

          1. Ensure Chrome is open and frontmost (Win32 API; no
             taskbar clicks).
          2. Maximize the foreground window via Win32 ShowWindow.
          3. Bump zoom to ~zoom_steps*10 % via Ctrl + mouse-wheel so
             small UI targets (back arrows, follow buttons) are easier
             for the vision model to localize. No reset to 100 % first
             — that requires Ctrl+0 which we're fine with, but the
             `=` key is avoided entirely.

        Why no keyboard zoom hotkeys (Ctrl+=, Win+Up, etc.): on CN
        Windows installs we kept tripping two third-party hotkey
        consumers:
          - UU加速器 / 网易UU registers global low-level keyboard
            hooks. Stray Win-key events (even a bare KEYUP from a
            "defensive modifier release") pop its accelerator UI.
          - pyautogui's hotkey delivery has no enforced gap between
            keyUp(modifier) and the next keyDown. The `=` from a
            subsequent Ctrl+= can land while Windows still has Win
            logically held, firing Win+= → Magnifier.

        Mouse-wheel zoom sidesteps both: no `=` key ever leaves
        SendInput, and there are no spurious modifier keystrokes."""
        self._clear_os_overlays()
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

        # Zoom via Ctrl + wheel. Chrome interprets each wheel tick with
        # Ctrl held as a 10 % zoom step. We park the cursor in the
        # window content area first because some apps only honor
        # Ctrl+wheel when the wheel event is delivered to a child of
        # the focused window.
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                cx = mon["width"] // 2
                cy = mon["height"] // 2
            pyautogui.moveTo(cx, cy, duration=0)
            time.sleep(0.05)
            pyautogui.keyDown("ctrl")
            time.sleep(0.05)
            for _ in range(max(0, zoom_steps)):
                pyautogui.scroll(1)        # one wheel tick up = zoom in
                time.sleep(0.12)
            pyautogui.keyUp("ctrl")
        except Exception:
            # Best-effort: a zoom failure shouldn't take down the task.
            try:
                pyautogui.keyUp("ctrl")
            except Exception:
                pass

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

    def scroll(self, direction: str = "down", clicks: int = 1,
               x: float | None = None, y: float | None = None):
        """Scroll the wheel by `clicks` events of `step` real detents each.

        WHEEL_DELTA note: pyautogui.scroll(n) passes `n` straight to the Win32
        `mouse_event(MOUSEEVENTF_WHEEL, dwData=n)` as the RAW wheel delta — it
        does NOT scale by WHEEL_DELTA (120 = one physical detent). So
        pyautogui.scroll(1) injects 1/120th of a notch — almost no movement,
        which is why list modals appeared unscrollable. We multiply by 120 so
        `step` means whole detents, matching a real wheel and behaving
        predictably across pages and modals.

        Calibration: on Douyin's follow-roster modal one detent ≈ 2-3 rows, so
        the default (1 detent/event, 1 event) advances ~half a viewport — far
        enough to make progress, with enough overlap that no list row is
        skipped. PHANTOM_SCROLL_STEP (detents/event) and `clicks` tune it."""
        if x is not None and y is not None:
            _humanized_move(x, y)
        sign = -1 if direction == "down" else 1
        try:
            step = int(os.environ.get("PHANTOM_SCROLL_STEP", "1") or 1)
        except ValueError:
            step = 1
        step = max(1, step)
        WHEEL_DELTA = 120  # one physical wheel detent
        for i in range(max(1, clicks)):
            pyautogui.scroll(sign * step * WHEEL_DELTA)
            if i < clicks - 1:
                # Pace multi-event scrolls a little so each registers and the
                # stream looks like real wheel inertia rather than one burst.
                time.sleep(max(0.05, humanize.scroll_step_delay_s()))


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
