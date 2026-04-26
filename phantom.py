#!/usr/bin/env python3
"""
Phantom Click — Undetectable AI-driven automation via CGEvent + Vision AI

Architecture:
  Screenshot (CGWindowListCreateImage) → Gemini Vision (identify targets) → CGEvent (hardware-level input)

CGEvent injects at the kernel level — browsers see isTrusted:true, indistinguishable from physical input.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import random
import math
from typing import Optional, Tuple, List

# ─── CGEvent Input Simulation (Quartz / CoreGraphics) ───────────────────

import Quartz
from Quartz import (
    CGEventCreateMouseEvent,
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetIntegerValueField,
    kCGEventMouseMoved,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventRightMouseDown,
    kCGEventRightMouseUp,
    kCGEventScrollWheel,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
    kCGHIDEventTap,
    kCGScrollEventUnitPixel,
    kCGEventSourceStateHIDSystemState,
    CGEventSourceCreate,
    CGEventCreateScrollWheelEvent,
)
from Quartz import CGWindowListCreateImage, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
from Quartz import CGRectInfinite, kCGWindowImageDefault
from CoreFoundation import CFDataGetBytes, CFDataGetLength
import objc


class PhantomInput:
    """Hardware-level input simulation via CGEvent.

    Events injected at kCGHIDEventTap are indistinguishable from physical
    mouse/keyboard input. Browsers see isTrusted:true.
    """

    def __init__(self):
        self._source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        self._current_x = 0.0
        self._current_y = 0.0

    def move_to(self, x: float, y: float, duration: float = 0.0):
        """Move cursor to (x, y). If duration > 0, animate with bezier curve."""
        if duration > 0:
            self._smooth_move(x, y, duration)
        else:
            point = Quartz.CGPointMake(x, y)
            event = CGEventCreateMouseEvent(self._source, kCGEventMouseMoved, point, kCGMouseButtonLeft)
            CGEventPost(kCGHIDEventTap, event)
            self._current_x, self._current_y = x, y

    def _smooth_move(self, target_x: float, target_y: float, duration: float):
        """Bezier curve mouse movement — mimics human hand motion."""
        start_x, start_y = self._current_x, self._current_y

        # Random control point for natural curve
        mid_x = (start_x + target_x) / 2 + random.uniform(-80, 80)
        mid_y = (start_y + target_y) / 2 + random.uniform(-80, 80)

        steps = max(20, int(duration * 60))  # ~60fps
        step_delay = duration / steps

        for i in range(steps + 1):
            t = i / steps
            # Quadratic bezier
            x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * mid_x + t ** 2 * target_x
            y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * mid_y + t ** 2 * target_y

            # Add micro-jitter (human hand tremor)
            jx = x + random.gauss(0, 0.5)
            jy = y + random.gauss(0, 0.5)

            point = Quartz.CGPointMake(jx, jy)
            event = CGEventCreateMouseEvent(self._source, kCGEventMouseMoved, point, kCGMouseButtonLeft)
            CGEventPost(kCGHIDEventTap, event)

            # Variable speed (slower at start and end)
            speed_factor = 0.5 + 0.5 * math.sin(t * math.pi)
            time.sleep(step_delay * (0.5 + speed_factor * 0.5))

        self._current_x, self._current_y = target_x, target_y

    def click(self, x: float, y: float, button: str = "left", smooth: bool = True):
        """Click at (x, y). Moves cursor smoothly first if smooth=True."""
        if smooth:
            dist = math.hypot(x - self._current_x, y - self._current_y)
            duration = min(0.8, max(0.15, dist / 1500))
            self.move_to(x, y, duration=duration)
        else:
            self.move_to(x, y)

        time.sleep(random.uniform(0.02, 0.08))

        point = Quartz.CGPointMake(x, y)

        if button == "right":
            down_type, up_type, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        else:
            down_type, up_type, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft

        down = CGEventCreateMouseEvent(self._source, down_type, point, btn)
        CGEventPost(kCGHIDEventTap, down)
        time.sleep(random.uniform(0.04, 0.12))  # Human press duration

        up = CGEventCreateMouseEvent(self._source, up_type, point, btn)
        CGEventPost(kCGHIDEventTap, up)

    def double_click(self, x: float, y: float):
        """Double-click at (x, y)."""
        self.click(x, y)
        time.sleep(random.uniform(0.05, 0.15))
        point = Quartz.CGPointMake(x, y)

        down = CGEventCreateMouseEvent(self._source, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
        CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, 2)
        CGEventPost(kCGHIDEventTap, down)
        time.sleep(random.uniform(0.04, 0.1))

        up = CGEventCreateMouseEvent(self._source, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
        CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, 2)
        CGEventPost(kCGHIDEventTap, up)

    def scroll(self, dx: int = 0, dy: int = -3):
        """Scroll. dy < 0 = scroll down, dy > 0 = scroll up."""
        event = CGEventCreateScrollWheelEvent(self._source, kCGScrollEventUnitPixel, 2, dy, dx)
        CGEventPost(kCGHIDEventTap, event)

    def type_text(self, text: str, wpm: int = 80):
        """Type text character by character at realistic speed."""
        chars_per_sec = (wpm * 5) / 60  # avg 5 chars per word
        base_delay = 1.0 / chars_per_sec

        for char in text:
            keycode, shift = self._char_to_keycode(char)
            if keycode is None:
                continue

            if shift:
                # Press shift
                shift_down = CGEventCreateKeyboardEvent(self._source, 56, True)
                CGEventPost(kCGHIDEventTap, shift_down)
                time.sleep(0.02)

            key_down = CGEventCreateKeyboardEvent(self._source, keycode, True)
            CGEventPost(kCGHIDEventTap, key_down)
            time.sleep(random.uniform(0.03, 0.08))

            key_up = CGEventCreateKeyboardEvent(self._source, keycode, False)
            CGEventPost(kCGHIDEventTap, key_up)

            if shift:
                shift_up = CGEventCreateKeyboardEvent(self._source, 56, False)
                CGEventPost(kCGHIDEventTap, shift_up)

            # Variable typing speed
            delay = base_delay * random.uniform(0.6, 1.8)
            if char in ".!?\n":
                delay += random.uniform(0.1, 0.4)  # Pause after punctuation
            time.sleep(delay)

    def press_key(self, keycode: int, modifiers: List[int] = None):
        """Press a key with optional modifiers."""
        modifiers = modifiers or []

        # Press modifiers
        for mod in modifiers:
            down = CGEventCreateKeyboardEvent(self._source, mod, True)
            CGEventPost(kCGHIDEventTap, down)
            time.sleep(0.02)

        # Press key
        down = CGEventCreateKeyboardEvent(self._source, keycode, True)
        CGEventPost(kCGHIDEventTap, down)
        time.sleep(random.uniform(0.04, 0.1))
        up = CGEventCreateKeyboardEvent(self._source, keycode, False)
        CGEventPost(kCGHIDEventTap, up)

        # Release modifiers (reverse order)
        for mod in reversed(modifiers):
            up = CGEventCreateKeyboardEvent(self._source, mod, False)
            CGEventPost(kCGHIDEventTap, up)
            time.sleep(0.02)

    @staticmethod
    def _char_to_keycode(char: str) -> Tuple[Optional[int], bool]:
        """Map character to macOS keycode + shift flag."""
        # Common US keyboard layout keycodes
        keymap = {
            'a': (0, False), 'b': (11, False), 'c': (8, False), 'd': (2, False),
            'e': (14, False), 'f': (3, False), 'g': (5, False), 'h': (4, False),
            'i': (34, False), 'j': (38, False), 'k': (40, False), 'l': (37, False),
            'm': (46, False), 'n': (45, False), 'o': (31, False), 'p': (35, False),
            'q': (12, False), 'r': (15, False), 's': (1, False), 't': (17, False),
            'u': (32, False), 'v': (9, False), 'w': (13, False), 'x': (7, False),
            'y': (16, False), 'z': (6, False),
            '1': (18, False), '2': (19, False), '3': (20, False), '4': (21, False),
            '5': (23, False), '6': (22, False), '7': (26, False), '8': (28, False),
            '9': (25, False), '0': (29, False),
            ' ': (49, False), '\n': (36, False), '\t': (48, False),
            '-': (27, False), '=': (24, False), '[': (33, False), ']': (30, False),
            '\\': (42, False), ';': (41, False), "'": (39, False), ',': (43, False),
            '.': (47, False), '/': (44, False), '`': (50, False),
            # Shifted characters
            '!': (18, True), '@': (19, True), '#': (20, True), '$': (21, True),
            '%': (23, True), '^': (22, True), '&': (26, True), '*': (28, True),
            '(': (25, True), ')': (29, True), '_': (27, True), '+': (24, True),
            '{': (33, True), '}': (30, True), '|': (42, True), ':': (41, True),
            '"': (39, True), '<': (43, True), '>': (47, True), '?': (44, True),
            '~': (50, True),
        }
        lower = char.lower()
        if lower in keymap:
            code, shift = keymap[lower]
            if char.isupper():
                shift = True
            return code, shift
        return None, False

    def paste_text(self, text: str):
        """Place text on the macOS pasteboard, then send Cmd+V (real CGEvent).
        Use for non-ASCII (e.g. Chinese) where _char_to_keycode can't map."""
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        time.sleep(random.uniform(0.08, 0.18))
        # 9 = V, 55 = Cmd
        self.press_key(9, [55])


# ─── Screenshot ─────────────────────────────────────────────────────────

class PhantomScreen:
    """Capture screen using CGWindowListCreateImage (Quartz)."""

    @staticmethod
    def capture(output_path: str = None, region: Tuple[int, int, int, int] = None) -> str:
        """Capture the entire screen or a region. Returns path to PNG."""
        if output_path is None:
            output_path = os.path.join(tempfile.gettempdir(), f"phantom_screen_{int(time.time())}.png")

        if region:
            x, y, w, h = region
            rect = Quartz.CGRectMake(x, y, w, h)
        else:
            rect = CGRectInfinite

        image = CGWindowListCreateImage(
            rect,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowImageDefault,
        )

        if image is None:
            raise RuntimeError("Screenshot failed — check Screen Recording permission")

        # Save as PNG using sips (simpler than manual PNG encoding)
        # First save as TIFF via CGImage, then convert
        from AppKit import NSBitmapImageRep, NSPNGFileType
        rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
        png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
        png_data.writeToFile_atomically_(output_path, True)

        return output_path

    @staticmethod
    def get_screen_size() -> Tuple[int, int]:
        """Get main display logical resolution (points, not pixels)."""
        main = Quartz.CGMainDisplayID()
        bounds = Quartz.CGDisplayBounds(main)
        w = int(bounds.size.width)
        h = int(bounds.size.height)
        return w, h


# ─── Vision (Claude) ────────────────────────────────────────────────────
#
# phantom-click uses CLAUDE'S vision exclusively. Gemini support has been
# removed — it was unreliable for dense UI (off-by-one tabs, wrong cards)
# and slow (30–90s/call via web-UI scraping).
#
# Two supported modes:
#
#   1) INTERACTIVE (default, no API key needed) — Claude in your Claude Code
#      chat IS the vision model. Workflow:
#         a) `phantom.py screenshot -o /tmp/s.png`
#         b) Claude reads /tmp/s.png and decides coords
#         c) `phantom.py click X Y` (or any other primitive)
#      The PhantomAgent autopilot loop is REMOVED in this mode — Claude
#      drives the loop turn-by-turn from the chat instead.
#
#   2) API (set ANTHROPIC_API_KEY) — phantom.py shells out to the Anthropic
#      Messages API with the screenshot attached. Lets you run unattended.
#      Implemented in `claude_vision.py`.
#
# Either way: NEVER call Gemini. The old `gemini_vision.py` is now a
# deprecation stub.

class ClaudeVision:
    """Vision via Claude (multimodal). Two modes: interactive or API.

    See module-level comment above for usage. The old `PhantomVision` /
    Gemini path is gone and will not be reintroduced.
    """

    VISION_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_vision.py")

    @staticmethod
    def analyze(screenshot_path: str, instruction: str, **_kwargs) -> dict:
        """Run Claude vision on a screenshot, return {steps:[...]}.

        Requires ANTHROPIC_API_KEY for autopilot. For interactive use within
        a Claude Code chat, do NOT call this — instead, save the screenshot
        and let Claude (in chat) read it and emit the click commands.
        """
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {
                "action": "error",
                "reasoning": (
                    "Vision requires Claude. Either set ANTHROPIC_API_KEY for "
                    "autopilot mode, or use phantom-click interactively from a "
                    "Claude Code chat — let Claude read the screenshot and emit "
                    "`phantom.py click X Y` commands directly. Gemini is not used."
                ),
            }
        try:
            result = subprocess.run(
                ["python3", ClaudeVision.VISION_HELPER, screenshot_path, instruction],
                capture_output=True, text=True, timeout=60,
            )
            response = result.stdout.strip()
            if not response:
                return {"action": "error", "reasoning": "Claude API returned empty"}
            import re
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return json.loads(json_match.group())
            return {"action": "error", "reasoning": f"Could not parse JSON: {response[:200]}"}
        except subprocess.TimeoutExpired:
            return {"action": "error", "reasoning": "Claude API timed out"}
        except Exception as e:
            return {"action": "error", "reasoning": str(e)}


# Back-compat alias: anything that imported `PhantomVision` still works,
# but it now routes through ClaudeVision (which refuses Gemini).
PhantomVision = ClaudeVision


# ─── Phantom Agent ──────────────────────────────────────────────────────

class PhantomAgent:
    """Autopilot: screenshot → Claude vision → action loop.

    Only useful with ANTHROPIC_API_KEY set. For interactive use within a
    Claude Code chat, skip this class — Claude in chat is your loop.
    """

    def __init__(self, verbose: bool = True):
        self.input = PhantomInput()
        self.screen = PhantomScreen()
        self.vision = ClaudeVision()
        self.verbose = verbose
        self.max_steps = 15
        self.screenshot_dir = tempfile.mkdtemp(prefix="phantom_")

        # Detect Retina scale factor
        screen_w, screen_h = self.screen.get_screen_size()
        test_path = os.path.join(self.screenshot_dir, "_scale_test.png")
        self.screen.capture(test_path)
        try:
            from PIL import Image
            img = Image.open(test_path)
            self.scale_factor = img.size[0] / screen_w
        except:
            self.scale_factor = 2.0  # Default Retina
        os.unlink(test_path)
        self.log(f"Screen: {screen_w}x{screen_h}, scale: {self.scale_factor}x")

    def log(self, msg: str):
        if self.verbose:
            print(f"[Phantom] {msg}")

    def _execute_action(self, action: dict):
        """Execute a single action dict."""
        act = action.get("action", "error")

        if act == "click":
            x = float(action["x"]) / self.scale_factor
            y = float(action["y"]) / self.scale_factor
            self.log(f"  Click ({x:.0f}, {y:.0f}) — {action.get('reasoning', '')}")
            self.input.click(x, y)

        elif act == "double_click":
            x = float(action["x"]) / self.scale_factor
            y = float(action["y"]) / self.scale_factor
            self.log(f"  Double-click ({x:.0f}, {y:.0f})")
            self.input.double_click(x, y)

        elif act == "type":
            text = action.get("text", "")
            self.log(f"  Type: {text[:50]}...")
            if any(ord(c) > 127 for c in text):
                self.input.paste_text(text)
            else:
                self.input.type_text(text)

        elif act == "key":
            key_name = action.get("key", "")
            keymap = {
                "enter": 36, "return": 36, "tab": 48, "escape": 53,
                "space": 49, "backspace": 51, "delete": 117,
                "up": 126, "down": 125, "left": 123, "right": 124,
                "a": 0, "c": 8, "v": 9, "x": 7, "z": 6, "s": 1, "f": 3,
            }
            mod_map = {
                "command": 55, "cmd": 55, "shift": 56, "option": 58,
                "alt": 58, "control": 59, "ctrl": 59,
            }
            parts = [p.strip().lower() for p in key_name.lower().split("+")]
            modifiers = [mod_map[p] for p in parts if p in mod_map]
            main_key = next((keymap[p] for p in parts if p in keymap), None)

            if main_key is not None:
                self.log(f"  Key: {key_name}")
                self.input.press_key(main_key, modifiers=modifiers if modifiers else None)
            else:
                self.log(f"  Unknown key: {key_name}")

        elif act == "scroll":
            direction = action.get("direction", "down")
            dy = -5 if direction == "down" else 5
            self.log(f"  Scroll {direction}")
            self.input.scroll(dy=dy)

    def execute(self, task: str) -> bool:
        """Execute a task by repeatedly: screenshot → analyze → act."""
        self.log(f"Task: {task}")

        for step in range(self.max_steps):
            self.log(f"\n--- Step {step + 1}/{self.max_steps} ---")

            # 1) Screenshot
            screenshot_path = os.path.join(self.screenshot_dir, f"step_{step}.png")
            self.screen.capture(screenshot_path)
            self.log(f"Screenshot: {screenshot_path}")

            # 2) Ask Claude what to do (Gemini removed)
            self.log("Analyzing with Claude vision...")
            response = self.vision.analyze(screenshot_path, task)
            self.log(f"Response: {json.dumps(response, ensure_ascii=False)[:200]}")

            # 3) Execute actions — support both single action and steps array
            steps = response.get("steps", [response])  # fallback: treat whole response as single action

            for i, action in enumerate(steps):
                act = action.get("action", "error")

                if act == "done":
                    self.log("Task completed!")
                    return True
                elif act == "error":
                    self.log(f"Error: {action.get('reasoning', 'unknown')}")
                    return False
                else:
                    self._execute_action(action)
                    # Short delay between sequential sub-actions
                    if i < len(steps) - 1:
                        time.sleep(random.uniform(0.3, 0.8))

            # Wait between major steps
            time.sleep(random.uniform(0.5, 1.5))

        self.log("Max steps reached without completing task")
        return False


# ─── CLI ────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phantom Click — Undetectable AI automation")
    sub = parser.add_subparsers(dest="command")

    # click
    p_click = sub.add_parser("click", help="Click at coordinates")
    p_click.add_argument("x", type=float)
    p_click.add_argument("y", type=float)

    # type
    p_type = sub.add_parser("type", help="Type text")
    p_type.add_argument("text")

    # screenshot
    p_ss = sub.add_parser("screenshot", help="Take screenshot")
    p_ss.add_argument("-o", "--output", default=None)

    # run
    p_run = sub.add_parser("run", help="Run a task with AI agent")
    p_run.add_argument("task", help="Natural language task description")
    p_run.add_argument("--max-steps", type=int, default=15)

    args = parser.parse_args()

    if args.command == "click":
        pi = PhantomInput()
        pi.click(args.x, args.y)
        print(f"Clicked at ({args.x}, {args.y})")

    elif args.command == "type":
        pi = PhantomInput()
        pi.type_text(args.text)
        print(f"Typed: {args.text}")

    elif args.command == "screenshot":
        ps = PhantomScreen()
        path = ps.capture(args.output)
        print(f"Screenshot saved: {path}")

    elif args.command == "run":
        agent = PhantomAgent()
        agent.max_steps = args.max_steps
        success = agent.execute(args.task)
        sys.exit(0 if success else 1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
