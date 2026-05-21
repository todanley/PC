"""Slider-jigsaw CAPTCHA assist.

Douyin (and many CN sites) gate automation behind a "drag the piece into the
matching shadow" slider puzzle. The vision model fails it because `drag` is a
pure coordinate-regression task — it can't pixel-localize the slider/gap.

This module splits the problem along each component's strength:
  • image processing (here) locates the puzzle PIECE and the candidate GAP
    shapes at the pixel level — precision the model lacks;
  • Set-of-Mark (app/som.py) tags those shapes as numbered marks;
  • the model picks WHICH gap matches the piece's shape — semantics it's good
    at — by returning a mark-to-mark drag;
  • the runner drags between the two marks' exact centers (app/vision.py
    resolves from_mark/to_mark → x1,y1,x2,y2).

Best-effort: detection is reliable on clear puzzle backgrounds but can miss on
low-contrast ones (snow) or while the puzzle is still loading — in which case
no captcha marks are added and the model just waits / retries.

All heavy deps (cv2, numpy) are imported lazily so importing this module is
free in a bundle without the OCR/vision runtime.
"""

# Text fragments that indicate a verification/CAPTCHA screen (matched against
# the OCR text SoM already extracts each turn).
_HINTS = ("验证", "拼图", "滑块", "请完成", "拖动完成", "拖动滑块", "captcha", "verif")


def looks_like_captcha(texts) -> bool:
    """True if the OCR'd text on the screen looks like a slider CAPTCHA."""
    blob = " ".join(t for t in texts if t)
    return any(h in blob for h in _HINTS)


def _find_puzzle(cv2, frame_bgr):
    """The puzzle photo is the one large SATURATED rectangle on the otherwise
    plain verification card. Return (x,y,w,h) in frame px or None."""
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    sat = cv2.inRange(hsv[:, :, 1], 35, 255)
    sat = cv2.morphologyEx(sat, cv2.MORPH_CLOSE,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31)))
    cnts, _ = cv2.findContours(sat, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    fh, fw = frame_bgr.shape[:2]
    best = None
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        # Reject region that's basically the whole page (a colorful normal
        # page, not a puzzle card): a real puzzle photo is a bounded rectangle
        # well inside the verification card.
        if (w * h) > 0.70 * fw * fh or w > 0.85 * fw:
            continue
        if w > 250 and h > 140 and 1.2 < w / h < 3.0 and (best is None or w * h > best[4]):
            best = (x, y, w, h, w * h)
    return best[:4] if best else None


def _find_piece(cv2, edges, W, H):
    """Piece = the prominent edge-shape in the left ~30% of the puzzle (it's
    the outlined piece that always starts at the left)."""
    left = edges.copy()
    left[:, int(0.30 * W):] = 0
    cnts, _ = cv2.findContours(left, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if 0.05 * W < w < 0.30 * W and 0.18 * H < h < 0.75 * H and 0.5 < w / h < 1.9:
            if best is None or w * h > best[4]:
                best = (x, y, w, h, w * h)
    return best[:4] if best else None


def _template_gap(cv2, np, edges, piece, W, H):
    """Edge-based template match: slide the piece's edge patch across the
    region to its right; the peak is the matching gap. Returns gap center-x
    (puzzle-local) or None."""
    qx, qy, qw, qh = piece
    tmpl = edges[qy:qy + qh, qx:qx + qw].astype(np.float32)
    sx0 = qx + qw
    search = edges[:, sx0:].astype(np.float32)
    if search.shape[1] <= qw or search.shape[0] < qh:
        return None
    res = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED)
    _, _, _, maxloc = cv2.minMaxLoc(res)
    return sx0 + maxloc[0] + qw / 2


def detect(frame_bgr):
    """Locate the piece + candidate gaps on a slider-jigsaw CAPTCHA.

    Returns {'piece': (cx,cy), 'gaps': [(cx,cy), …], 'box': (x,y,w,h)} in
    FRAME pixel coords, or None if no solvable puzzle is found."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        box = _find_puzzle(cv2, frame_bgr)
        if not box:
            return None
        x, y, w, h = box
        puzzle = frame_bgr[y:y + h, x:x + w]
        g = cv2.GaussianBlur(cv2.cvtColor(puzzle, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        edges = cv2.Canny(g, 50, 150)
        H, W = g.shape
        piece = _find_piece(cv2, edges, W, H)
        if not piece:
            return None
        qx, qy, qw, qh = piece
        piece_c = (x + qx + qw / 2.0, y + qy + qh / 2.0)

        gaps = []
        # (a) contour candidates to the right of the piece
        edl = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), 2)
        cnts, _ = cv2.findContours(edl, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            cx, cy, cw, ch = cv2.boundingRect(c)
            if 0.05 * W < cw < 0.32 * W and 0.18 * H < ch < 0.75 * H and 0.5 < cw / ch < 1.9:
                if (cx + cw / 2) - (qx + qw / 2) > 0.10 * W:
                    gaps.append((x + cx + cw / 2.0, y + cy + ch / 2.0))
        # (b) the edge-template best match (robust single gap), at the piece's y
        tg = _template_gap(cv2, np, edges, piece, W, H)
        if tg is not None:
            gaps.append((x + tg, piece_c[1]))

        # dedupe gaps that are within ~5% of the width of each other
        merged = []
        tol = 0.05 * W
        for gx, gy in sorted(gaps, key=lambda p: p[0]):
            if not any(abs(gx - mx) < tol for mx, _ in merged):
                merged.append((gx, gy))
        if not merged:
            return None
        # Slider HANDLE: the drag must grab the handle in the track BELOW the
        # puzzle photo (you can't drag the piece directly). Estimate it at the
        # left of that track. Clamped to the image. (Live-calibratable.)
        fh, fw = frame_bgr.shape[:2]
        handle = (min(fw - 1, x + 0.06 * w),
                  min(fh - 1, y + h + 0.17 * h))
        return {"piece": piece_c, "gaps": merged, "handle": handle, "box": box}
    except Exception:
        return None
