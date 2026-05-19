"""Behavioral humanization layer.

Shapes OS-injected input (mouse, click, type, scroll, drag) and runner-level
action pacing so the resulting DOM event stream looks like a real person to
JS-side behavioral fingerprinting (e.g. douyin.com's bot detection). Does NOT
change *how* events are injected — that's still pyautogui / SendInput at the
Win32 level. Only changes the *shape* and *cadence* of what's emitted.

Implements the A1-A9 (input) + B1-B7 (pacing) shortlist from the threat-model
audit. Disabled in dev/test via PHANTOM_HUMANIZE=0.
"""
import json
import math
import os
import random
import time
from pathlib import Path

# Master switch.
ENABLED = os.environ.get("PHANTOM_HUMANIZE", "1") != "0"

# Action-rate cap. Casual desktop use is ~5-30 actions/min; active social-app
# browsing tops out around 30-40. 25 averages to one action every 2.4 s.
ACTIONS_PER_MIN = int(os.environ.get("PHANTOM_ACTIONS_PER_MIN", "25"))
# Tasks-per-hour cap, persisted across runs.
TASKS_PER_HOUR = int(os.environ.get("PHANTOM_TASKS_PER_HOUR", "20"))


# ── A3: click down-up dwell ─────────────────────────────────────────────────
def click_dwell_s() -> float:
    """Gap between mouseDown and mouseUp. Real humans: ~50-150 ms.
    pyautogui's bare click() is ~0 — a strong bot tell in JS-level event timing
    (mousedown.timeStamp vs mouseup.timeStamp histograms)."""
    if not ENABLED:
        return 0.0
    return max(0.035, min(0.180, random.gauss(0.095, 0.030)))


# ── A2: variable mouse-move duration (Fitts's-law-ish) ──────────────────────
def move_duration_s(distance_px: float) -> float:
    """Duration scales with target distance (~600 px/s peak speed) plus a
    fixed ~120 ms acquisition cost, with ±20 % per-move randomization."""
    if not ENABLED:
        return 0.0
    base = 0.120 + distance_px / 800.0
    return max(0.08, min(1.2, base * random.uniform(0.85, 1.20)))


# ── A4/A5: per-key delays for ASCII typing ──────────────────────────────────
# Common English bigrams transition faster than the median; awkward
# finger-combo bigrams transition slower. Multiplier on the log-normal base.
_BIGRAM_WEIGHTS = {
    "th": 0.60, "he": 0.65, "in": 0.65, "er": 0.65, "an": 0.65,
    "re": 0.70, "on": 0.70, "at": 0.70, "en": 0.70, "nd": 0.70,
    "ti": 0.70, "es": 0.70, "or": 0.70, "te": 0.75, "of": 0.75,
    "ed": 0.70, "is": 0.70, "it": 0.70, "al": 0.75, "ar": 0.75,
    "qx": 1.60, "zj": 1.60, "qz": 1.60, "vp": 1.40, "jq": 1.60,
    "xv": 1.40, "kx": 1.40, "wq": 1.40, "pz": 1.40, "fq": 1.40,
}


def key_delay_s(prev_char: str, next_char: str) -> float:
    """Inter-key delay for ASCII typing. Log-normal with bigram-aware
    multiplier; occasional thinking pauses (5 % chance, 200-700 ms)."""
    if not ENABLED:
        return 0.05
    base = math.exp(random.gauss(math.log(0.080), 0.45))
    if prev_char and next_char:
        weight = _BIGRAM_WEIGHTS.get((prev_char + next_char).lower(), 1.0)
        base *= weight
    if random.random() < 0.05:
        base += random.uniform(0.20, 0.70)
    return max(0.02, min(2.0, base))


# ── A6: typos + corrections ─────────────────────────────────────────────────
# QWERTY-adjacent keys per letter — what a real fat-finger typo looks like.
_QWERTY_NEIGHBORS = {
    "a": "sqzw", "b": "vghn", "c": "xdfv", "d": "serfc", "e": "wsdr",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn",
    "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iklp",
    "p": "ol", "q": "wa", "r": "edft", "s": "awdxz", "t": "rfgy",
    "u": "yhji", "v": "cfgb", "w": "qsae", "x": "zsdc", "y": "tghu",
    "z": "asx",
}


def should_typo(text: str) -> bool:
    """Should this typing run include one typo+correction? Probability
    scales with length so short inputs (search queries, single words) never
    get typos but longer sentences sometimes do. Capped at 20 %."""
    if not ENABLED or len(text) < 8:
        return False
    return random.random() < min(0.20, len(text) * 0.015)


def pick_typo(text: str) -> tuple[int, str]:
    """Pick a position (not first/last char) and a plausible wrong char.
    Returns (index, wrong_char). Caller types text[:i], then wrong_char,
    pauses, backspaces, types text[i:] from i onwards."""
    candidates = [i for i in range(1, len(text) - 1) if text[i].lower() in _QWERTY_NEIGHBORS]
    if not candidates:
        return (-1, "")
    i = random.choice(candidates)
    neighbors = _QWERTY_NEIGHBORS[text[i].lower()]
    wrong = random.choice(neighbors)
    if text[i].isupper():
        wrong = wrong.upper()
    return (i, wrong)


# ── A1: bezier mouse path with micro-jitter ─────────────────────────────────
def bezier_path(x1: float, y1: float, x2: float, y2: float,
                duration_s: float, n_points: int = 24
                ) -> list[tuple[float, float, float]]:
    """Cubic-bezier curve from (x1,y1) to (x2,y2) with per-point dwell. The
    curve has two random control points biased perpendicular to the straight
    line, giving a natural S-curve. Each interior point gets sub-pixel jitter
    so the resulting velocity profile has the micro-tremor of a real hand."""
    if not ENABLED or duration_s <= 0:
        return [(x2, y2, 0.0)]
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    px, py = -dy / dist, dx / dist
    curve = dist * random.uniform(0.05, 0.18) * random.choice([-1, 1])
    cx1 = x1 + dx * 0.3 + px * curve
    cy1 = y1 + dy * 0.3 + py * curve
    cx2 = x1 + dx * 0.7 + px * curve * random.uniform(0.4, 1.0)
    cy2 = y1 + dy * 0.7 + py * curve * random.uniform(0.4, 1.0)
    points: list[tuple[float, float, float]] = []
    dt_total = duration_s / n_points
    for i in range(1, n_points + 1):
        t = i / n_points
        u = 1 - t
        bx = u**3 * x1 + 3*u**2*t * cx1 + 3*u*t**2 * cx2 + t**3 * x2
        by = u**3 * y1 + 3*u**2*t * cy1 + 3*u*t**2 * cy2 + t**3 * y2
        if i < n_points:
            bx += random.gauss(0, 0.6)
            by += random.gauss(0, 0.6)
        # Ease-in/out timing: slower at endpoints, faster in middle.
        ease = 1.0 - abs(0.5 - t) * 1.4
        dt = max(0.0, dt_total * (0.5 + ease) * random.uniform(0.80, 1.20))
        points.append((bx, by, dt))
    return points


# ── A7: pixel-perfect-targeting tell ────────────────────────────────────────
def click_offset_px() -> tuple[float, float]:
    """Sub-pixel offset from the model's intended (x, y). Small enough
    (~±3 px @ 95 %) to stay inside any reasonably-sized button — back-arrow
    misses are caught by the runner's existing 8-fan-out retry."""
    if not ENABLED:
        return (0.0, 0.0)
    return (random.gauss(0, 1.8), random.gauss(0, 1.8))


# ── A8: pre-click hover ─────────────────────────────────────────────────────
def pre_click_hover_s() -> float:
    """30 % of clicks get a 100-700 ms hover before mouseDown — mimics the
    'is this the right button?' pause real users do."""
    if not ENABLED:
        return 0.0
    if random.random() < 0.30:
        return random.uniform(0.10, 0.70)
    return 0.0


# ── A9: variable-speed scroll ───────────────────────────────────────────────
def scroll_step_delay_s() -> float:
    """Delay between individual wheel ticks during a multi-tick scroll. Real
    wheel scrolling has momentum-driven inertia; pyautogui's single bulk call
    dispatches them simultaneously."""
    if not ENABLED:
        return 0.0
    return random.uniform(0.03, 0.12)


# ── B1/B2: inter-action pause ───────────────────────────────────────────────
def inter_action_pause_s(action_type: str) -> float:
    """Gap between dispatched actions. Type/drag get a longer 'thinking'
    prefix; scroll is fast and casual; everything else is 200-2000 ms."""
    if not ENABLED:
        return 0.0
    if action_type == "scroll":
        return random.uniform(0.10, 0.60)
    base = random.uniform(0.20, 2.0)
    if action_type in ("type", "drag"):
        base += random.uniform(0.8, 2.5)
    return base


# ── B4: idle micro-wiggle during pauses ─────────────────────────────────────
def idle_wiggle_px() -> tuple[float, float]:
    """Tiny cursor offset applied during inter-action pauses. Real cursors
    aren't perfectly still between clicks — micro-hand-tremor moves them
    a few pixels."""
    if not ENABLED:
        return (0.0, 0.0)
    return (random.gauss(0, 2.0), random.gauss(0, 2.0))


# ── B5: warm-up pause at task start ─────────────────────────────────────────
def warmup_pause_s() -> float:
    """Don't fire the first action exactly at t=0 — that's a robot signature.
    Real users take a moment to orient before doing anything."""
    if not ENABLED:
        return 0.0
    return random.uniform(1.5, 6.0)


# ── B6: sub-task breaks ─────────────────────────────────────────────────────
def maybe_subtask_break_s(actions_since_last_break: int) -> float:
    """Probabilistic 15-60 s pause every ~8-12 actions, simulating the
    'user got distracted' pattern that breaks otherwise-uniform pacing."""
    if not ENABLED or actions_since_last_break < 5:
        return 0.0
    p = min(0.30, (actions_since_last_break - 5) * 0.04)
    if random.random() < p:
        return random.uniform(15.0, 60.0)
    return 0.0


# ── B3: action-rate cap ─────────────────────────────────────────────────────
class RateLimiter:
    """Sliding-window cap on actions/min. .wait() blocks just long enough
    to keep the rate under max_per_min."""
    def __init__(self, max_per_min: int = ACTIONS_PER_MIN):
        self.max_per_min = max_per_min
        self._window: list[float] = []

    def wait(self):
        if not ENABLED:
            return
        now = time.time()
        cutoff = now - 60
        self._window = [t for t in self._window if t > cutoff]
        if len(self._window) >= self.max_per_min:
            sleep_for = self._window[0] - cutoff
            if sleep_for > 0:
                time.sleep(sleep_for + 0.05)
        self._window.append(time.time())


# ── B7: tasks-per-hour cap, persisted across runs ───────────────────────────
class TaskCounter:
    """Persists task-start timestamps so the hourly cap survives app
    restarts. Stored under %LOCALAPPDATA%\\PhantomClick on Windows,
    ~/Library/Application Support/PhantomClick on macOS."""
    def __init__(self, max_per_hour: int = TASKS_PER_HOUR):
        self.max_per_hour = max_per_hour
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA",
                                       str(Path.home() / "AppData" / "Local")))
        else:
            base = Path.home() / "Library" / "Application Support"
        self.path = base / "PhantomClick" / "task_count.json"

    def _load(self) -> list[float]:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return []

    def _save(self, ts: list[float]):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(ts))
        except Exception:
            pass

    def check_and_record(self) -> tuple[bool, int]:
        """Returns (allowed, count_in_last_hour). Records the timestamp
        when allowed."""
        if not ENABLED:
            return (True, 0)
        now = time.time()
        ts = [t for t in self._load() if t > now - 3600]
        if len(ts) >= self.max_per_hour:
            return (False, len(ts))
        ts.append(now)
        self._save(ts)
        return (True, len(ts))
