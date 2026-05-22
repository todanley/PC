"""Set-of-Mark (SoM) overlay: detect interactive elements on a screenshot,
draw numbered tags onto a copy of it, and return a map from tag id → click
point. Shifts the vision model's job from coordinate regression (hard) to
picking a numbered element (easy).

Reference: "Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding
in GPT-4V" (Yang, Zhang, Li, Zou, Li, Gao; 2023) —
https://som-gpt4v.github.io/ — original code at github.com/microsoft/SoM
(which leans on Semantic-SAM / SEEM for pixel-perfect segmentation). We use
the lighter hybrid the paper itself suggests for computer-use agents: an OCR
engine for text-bearing controls plus a contour pass for icon-only ones.

Element sources, both optional and lazily loaded:
  • OCR text boxes — RapidOCR running PaddleOCR PP-OCR models (Chinese +
    English) via ONNX Runtime. Catches labels, text buttons, menu items.
  • Icon / region proposals — OpenCV contours over an edge map. Catches the
    icon-only controls OCR can't see: back arrows, the heart / like glyph,
    the red "+" follow badge.

DESIGN RULE: SoM is a pure accuracy add-on, never load-bearing. If the OCR /
vision deps are absent or fail to load at runtime, `mark_screenshot` returns
an empty map and the caller falls back to plain coordinate prompting. It must
never raise into the run loop.

Coordinate space: every point returned is in the screenshot's own pixel
space — identical to the space the vision model sees, since `_encode_image`
sends the image without resizing. The caller converts to OS click-space the
same way it does for model-returned pixel coords.
"""
import os
import sys
import threading

# All heavy deps (cv2, numpy, rapidocr, PIL.ImageFont) are imported lazily
# inside the engine so importing this module is free and safe even in a
# bundle where the OCR runtime didn't ship.

# Tunables (env-overridable so the operator can dial precision/recall in the
# field without a rebuild).
_OCR_MIN_CONF = float(os.environ.get("PHANTOM_SOM_OCR_CONF", "0.45"))
_MAX_MARKS = int(os.environ.get("PHANTOM_SOM_MAX_MARKS", "120"))
# Icon proposals are noisier than OCR (contours fire on any textured
# background — game scenes, photos, video frames), so they get their own
# smaller budget and are only added AFTER all text marks. Set
# PHANTOM_SOM_ICONS=0 to disable the icon pass entirely for text-only UIs.
_ICONS_ENABLED = os.environ.get("PHANTOM_SOM_ICONS", "1") == "1"
_MAX_ICONS = int(os.environ.get("PHANTOM_SOM_MAX_ICONS", "40"))
# Icon proposals: keep boxes roughly button/icon sized, drop page-spanning
# rectangles and sub-glyph noise. Values are in image pixels.
_ICON_MIN_SIDE = int(os.environ.get("PHANTOM_SOM_ICON_MIN", "18"))
_ICON_MAX_SIDE = int(os.environ.get("PHANTOM_SOM_ICON_MAX", "140"))
# An icon candidate's bbox must have an edge-pixel density in this band: too
# sparse → scattered background texture (LoL terrain, photos); too dense →
# solid text block OCR already owns. UI glyphs sit in the middle.
_ICON_MIN_DENSITY = float(os.environ.get("PHANTOM_SOM_ICON_MIN_DENSITY", "0.08"))
_ICON_MAX_DENSITY = float(os.environ.get("PHANTOM_SOM_ICON_MAX_DENSITY", "0.55"))
# Two boxes whose centers are within this many px are treated as the same
# element (dedup OCR vs icon proposals, and near-duplicate contours).
_DEDUP_DIST = int(os.environ.get("PHANTOM_SOM_DEDUP_PX", "22"))

# UI Automation (Windows) element source: reads Chrome's accessibility tree at
# the OS level (detection-free — invisible to page JS) for precise boxes on
# semantic controls that OCR/icons miss (e.g. a tiny "···" more-button). Boxes
# come back in screen coords == screenshot-pixel space. Default OFF; the dev
# harness enables it (PHANTOM_SOM_UIA=1). Windows-only; lazy-loaded.
_UIA_ENABLED = os.environ.get("PHANTOM_SOM_UIA", "0") == "1"
_UIA_MAX = int(os.environ.get("PHANTOM_SOM_UIA_MAX", "80"))
_UIA_BUDGET_S = float(os.environ.get("PHANTOM_SOM_UIA_BUDGET", "1.0"))
_UIA_MAX_DEPTH = int(os.environ.get("PHANTOM_SOM_UIA_DEPTH", "25"))


class _Box:
    __slots__ = ("x0", "y0", "x1", "y1", "label", "kind")

    def __init__(self, x0, y0, x1, y1, label="", kind="text"):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.label = label
        self.kind = kind  # "text" | "icon"

    @property
    def cx(self):
        return (self.x0 + self.x1) // 2

    @property
    def cy(self):
        return (self.y0 + self.y1) // 2

    @property
    def area(self):
        return max(0, self.x1 - self.x0) * max(0, self.y1 - self.y0)


class SetOfMarkEngine:
    """Stateful so the (expensive) OCR model loads once and is reused across
    turns. Thread-safe lazy init: the runner builds one engine per run and
    calls `mark_screenshot` once per turn from its worker thread."""

    def __init__(self):
        self._ocr = None
        self._ocr_failed = False
        self._lock = threading.Lock()
        # Set each turn by mark_screenshot: the slider-CAPTCHA geometry
        # (piece/gaps/handle/box) when a captcha is on screen, else None. The
        # runner reads this to execute a `slide_captcha` action.
        self.last_captcha = None
        # Cached primary-monitor origin/size for mapping UIA screen rects into
        # screenshot-pixel space (computed once, lazily).
        self._uia_geom = None

    # ---- engine loading -------------------------------------------------

    def _get_ocr(self):
        """Lazily construct the RapidOCR reader. Returns the reader or None
        if the dependency is missing / fails to initialize (we only try
        once — `_ocr_failed` latches so we don't pay the import cost every
        turn on a box without the runtime)."""
        if self._ocr is not None or self._ocr_failed:
            return self._ocr
        with self._lock:
            if self._ocr is not None or self._ocr_failed:
                return self._ocr
            try:
                from rapidocr_onnxruntime import RapidOCR
                # Default bundled models are PP-OCRv4 (strong Chinese+English).
                # det/rec run on CPU via onnxruntime — no GPU needed.
                self._ocr = RapidOCR()
            except Exception:
                self._ocr_failed = True
                self._ocr = None
            return self._ocr

    # ---- detection ------------------------------------------------------

    def _detect_text(self, image_bgr) -> list:
        ocr = self._get_ocr()
        if ocr is None:
            return []
        try:
            # RapidOCR accepts an ndarray; returns (result, elapse) where
            # result is a list of [box(4 pts), text, confidence] or None.
            result, _ = ocr(image_bgr)
        except Exception:
            return []
        boxes = []
        for item in (result or []):
            try:
                quad, text, conf = item[0], item[1], float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            if conf < _OCR_MIN_CONF:
                continue
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            boxes.append(_Box(int(min(xs)), int(min(ys)),
                              int(max(xs)), int(max(ys)),
                              label=str(text).strip(), kind="text"))
        return boxes

    def _detect_icons(self, image_bgr) -> list:
        """Propose icon-sized clickable regions via contour analysis. This is
        deliberately permissive on recall (better to over-mark an icon than
        miss the only back-arrow on screen); the dedup pass downstream and a
        size band keep the clutter bounded."""
        try:
            import cv2
            import numpy as np
        except Exception:
            return []
        try:
            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            # Edge map → dilate so an icon's strokes merge into one blob,
            # then grab external contours as candidate element bounds.
            edges = cv2.Canny(gray, 60, 180)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            dilated = cv2.dilate(edges, kernel, iterations=2)
            contours, _ = cv2.findContours(
                dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
        except Exception:
            return []
        boxes = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if not (_ICON_MIN_SIDE <= w <= _ICON_MAX_SIDE
                    and _ICON_MIN_SIDE <= h <= _ICON_MAX_SIDE):
                continue
            # Icons are roughly square-ish; drop long thin slivers (those are
            # usually text underlines / borders OCR already owns).
            ar = w / float(h)
            if ar < 0.4 or ar > 2.5:
                continue
            # Density gate: reject scattered background texture (sparse) and
            # solid text blocks (dense). Measured on the raw edge map (not the
            # dilated one) so the threshold reflects real stroke coverage.
            try:
                cell = edges[y:y + h, x:x + w]
                density = float(cell.mean()) / 255.0  # fraction of edge px
            except Exception:
                density = 0.0
            if not (_ICON_MIN_DENSITY <= density <= _ICON_MAX_DENSITY):
                continue
            boxes.append(_Box(x, y, x + w, y + h, label="", kind="icon"))
        return boxes

    # ---- merge + draw ---------------------------------------------------

    @staticmethod
    def _dedup_against(candidates: list, existing: list) -> list:
        """Greedy near-duplicate filter: keep each candidate only if its
        center isn't within _DEDUP_DIST of an already-kept box (in `existing`)
        or an earlier-kept candidate. Larger boxes are offered first so the
        more clickable region survives a collision."""
        kept = list(existing)
        out = []
        for b in sorted(candidates, key=lambda b: -b.area):
            dup = False
            for k in kept:
                if (abs(b.cx - k.cx) <= _DEDUP_DIST
                        and abs(b.cy - k.cy) <= _DEDUP_DIST):
                    dup = True
                    break
            if not dup:
                kept.append(b)
                out.append(b)
        return out

    def _uia_origin_and_size(self):
        """Primary-monitor (left, top) and (width, height), cached. UIA returns
        absolute screen rects; subtracting this origin lands them in the mss
        screenshot's pixel space (mss grabs monitors[1])."""
        if self._uia_geom is not None:
            return self._uia_geom
        origin, size = (0, 0), None
        try:
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1]
                origin = (int(mon["left"]), int(mon["top"]))
                size = (int(mon["width"]), int(mon["height"]))
        except Exception:
            origin, size = (0, 0), None
        self._uia_geom = (origin, size)
        return self._uia_geom

    def _detect_uia(self) -> list:
        """Interactive elements of the focused Chrome window, read via Windows
        UI Automation (the accessibility tree). Boxes come back in
        screenshot-pixel space. Windows-only; returns [] when disabled,
        off-Windows, the dep is missing, or on any failure (never raises)."""
        if not _UIA_ENABLED or sys.platform != "win32":
            return []
        try:
            from . import uia_win
        except Exception:
            return []
        try:
            origin, size = self._uia_origin_and_size()
            rects = uia_win.collect_chrome_elements(
                max_elements=_UIA_MAX,
                time_budget_s=_UIA_BUDGET_S,
                max_depth=_UIA_MAX_DEPTH,
                origin_xy=origin,
                screen_wh=size,
            )
        except Exception:
            return []
        boxes = []
        for (x0, y0, x1, y1, name, _ctype) in rects:
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue
            boxes.append(_Box(int(x0), int(y0), int(x1), int(y1),
                              label=str(name)[:40], kind="uia"))
        return boxes

    def _merge(self, captcha_boxes: list, uia_boxes: list,
               text_boxes: list, icon_boxes: list) -> list:
        """Combine sources under a strict priority: CAPTCHA shapes first (they
        must never be dropped), then UIA accessibility boxes (precise + labeled
        — they SUPERSEDE a nearby OCR glyph, whose centroid is off for an
        icon+label control), then text marks, then icon marks fill whatever
        budget is left. Deduped so a lower-priority box never shadows a higher
        one. Icons get their own cap so a noisy contour pass can't flood it."""
        captcha_kept = self._dedup_against(captcha_boxes, [])[:_MAX_MARKS]
        uia_kept = self._dedup_against(uia_boxes, captcha_kept)
        uia_kept = uia_kept[:max(0, _MAX_MARKS - len(captcha_kept))]
        used = captcha_kept + uia_kept
        text_kept = self._dedup_against(text_boxes, used)
        text_kept = text_kept[:max(0, _MAX_MARKS - len(used))]
        used = used + text_kept
        budget = max(0, _MAX_MARKS - len(used))
        icon_kept = self._dedup_against(icon_boxes, used)
        icon_kept = icon_kept[:min(budget, _MAX_ICONS)]
        return captcha_kept + uia_kept + text_kept + icon_kept

    def mark_screenshot(self, image_path: str, output_path: str) -> dict:
        """Detect elements on `image_path`, write a tagged copy to
        `output_path`, and return {tag_id_str: (center_x, center_y)} in
        image-pixel space. Returns {} on any failure (caller falls back to
        plain coordinate prompting and should send the ORIGINAL image)."""
        try:
            import cv2
            import numpy as np
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return {}

        try:
            pil_src = Image.open(image_path).convert("RGB")
        except Exception:
            return {}
        image_bgr = cv2.cvtColor(np.array(pil_src), cv2.COLOR_RGB2BGR)

        text_boxes = self._detect_text(image_bgr)

        # CAPTCHA assist: if the OCR text says this is a verification/slider
        # puzzle, locate the piece + candidate gaps and tag them as priority
        # marks so the model can pick the matching pair for a `slide_captcha`.
        self.last_captcha = None
        captcha_boxes = []
        try:
            from . import captcha as _captcha
            if _captcha.looks_like_captcha([b.label for b in text_boxes]):
                det = _captcha.detect(image_bgr)
                if det:
                    self.last_captcha = det
                    r = 30
                    pcx, pcy = det["piece"]
                    captcha_boxes.append(_Box(int(pcx - r), int(pcy - r),
                                              int(pcx + r), int(pcy + r),
                                              label="piece", kind="captcha"))
                    for gx, gy in det["gaps"]:
                        captcha_boxes.append(_Box(int(gx - r), int(gy - r),
                                                  int(gx + r), int(gy + r),
                                                  label="gap", kind="captcha"))
        except Exception:
            self.last_captcha = None

        # UIA accessibility boxes (Windows, reads the live Chrome a11y tree).
        # Suppressed on captcha screens (like icons) — a verification page has
        # no useful page tree and we want the puzzle marks to stand out.
        uia_boxes = self._detect_uia() if not captcha_boxes else []

        # On a captcha screen suppress the noisy icon pass so the puzzle marks
        # stand out; otherwise run icons as usual.
        icon_boxes = (self._detect_icons(image_bgr)
                      if _ICONS_ENABLED and not captcha_boxes else [])
        boxes = self._merge(captcha_boxes, uia_boxes, text_boxes, icon_boxes)
        if not boxes:
            return {}
        # Reading order: top-to-bottom, then left-to-right, bucketed into ~24px
        # rows so ids feel natural to a human reading the marked image and are
        # stable when the layout barely shifts between turns.
        boxes.sort(key=lambda b: (b.cy // 24, b.cx))

        draw = ImageDraw.Draw(pil_src, "RGBA")
        try:
            font = ImageFont.truetype("arial.ttf", 15)
        except Exception:
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 15)
            except Exception:
                font = ImageFont.load_default()

        mark_map: dict = {}
        for idx, b in enumerate(boxes, start=1):
            mark_map[str(idx)] = (b.cx, b.cy)
            # Outline the element so the model can see exactly what a tag
            # refers to (magenta reads on light and dark UIs alike).
            draw.rectangle([b.x0, b.y0, b.x1, b.y1],
                           outline=(255, 0, 128, 255), width=2)
            label = f"{idx}"
            try:
                tw = draw.textlength(label, font=font)
            except Exception:
                tw = 8 * len(label)
            th = 16
            # Badge above the box top-left; clamp inside the image so edge
            # elements still show their tag.
            bx = max(0, b.x0)
            by = max(0, b.y0 - th - 1)
            draw.rectangle([bx, by, bx + tw + 6, by + th],
                           fill=(255, 0, 128, 230))
            draw.text((bx + 3, by + 1), label, fill=(255, 255, 255, 255),
                      font=font)

        try:
            pil_src.save(output_path)
        except Exception:
            return {}
        return mark_map


# Module-level singleton so repeated runs in one process reuse the loaded OCR
# model. The runner can also construct its own instance; both are fine.
_DEFAULT_ENGINE: SetOfMarkEngine | None = None


def get_engine() -> SetOfMarkEngine:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = SetOfMarkEngine()
    return _DEFAULT_ENGINE


def enabled() -> bool:
    """SoM is ON by default; set PHANTOM_SOM=0 to disable (falls back to
    plain coordinate prompting). The engine still degrades gracefully if the
    OCR runtime is unavailable, so 'on' is safe even where deps are missing."""
    return os.environ.get("PHANTOM_SOM", "1") != "0"
