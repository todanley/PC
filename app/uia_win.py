"""Windows UI Automation reader for Set-of-Mark.

Reads interactive elements of the FOREGROUND window (whatever app the user's
task is driving — a browser, the Douyin desktop app, a dialog, …) plus the
desktop icons, from the OS accessibility tree (UI Automation), and returns
their screen-space bounding boxes. This is DETECTION-FREE: it reads the a11y
tree at the OS level, invisible to any app's JavaScript, and injects nothing
(clicks still go through real OS mouse events elsewhere). Used by app/som.py
`_detect_uia` to mark small/semantic controls — e.g. a tiny "···" button, or a
desktop app icon to launch — that OCR and the icon contour pass miss.

It is APP-AGNOSTIC: no hardcoded app. The only app-specific touch is an
optional optimization — for a Chromium window we read the page Document subtree
to skip browser-chrome clutter; every other window is read whole.

Windows-only. All COM/comtypes work is isolated here and imported lazily, so
importing this module is cheap and it degrades to [] anywhere it can't run.
"""
import time

# Chromium top-level window class — used ONLY to pick the page-Document
# optimization, not to restrict which apps we read.
_CHROME_CLASS = "Chrome_WidgetWin_1"

# UIA ControlType names we consider "interactive" / worth marking. Resolved to
# their integer IDs from the generated comtypes module at call time. ListItem
# covers desktop icons + roster rows; the rest cover normal controls.
_INTERACTIVE_TYPE_NAMES = (
    "UIA_ButtonControlTypeId", "UIA_HyperlinkControlTypeId",
    "UIA_EditControlTypeId", "UIA_MenuItemControlTypeId",
    "UIA_CheckBoxControlTypeId", "UIA_RadioButtonControlTypeId",
    "UIA_ListItemControlTypeId", "UIA_TabItemControlTypeId",
    "UIA_ComboBoxControlTypeId", "UIA_SplitButtonControlTypeId",
    "UIA_MenuControlTypeId", "UIA_ImageControlTypeId",
)


# Win11 shell surfaces (Start menu, Search) are overlays owned by these
# processes; GetForegroundWindow() doesn't return them, so we find them by
# owning process and read them explicitly.
_SHELL_SURFACE_PROCS = ("startmenuexperiencehost.exe", "searchhost.exe",
                        "searchapp.exe")


def _proc_name(hwnd) -> str:
    """Lowercased exe name owning hwnd, '' on failure. ctypes-only (no extra
    pywin32 dep) using PROCESS_QUERY_LIMITED_INFORMATION (no elevation)."""
    try:
        import ctypes
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_ulong(512)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _shell_surface_hwnds(win32gui):
    """Visible Start-menu / Search overlay windows (Win11), found by owning
    process — GetForegroundWindow() won't surface them, so their items
    (Recent apps incl. a tiny '抖音', pinned tiles, the search box) otherwise
    go unmarked."""
    out = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            l, t, r, b = win32gui.GetWindowRect(h)
            if (r - l) < 200 or (b - t) < 200:   # skip tiny/hidden surfaces
                return True
            if _proc_name(h) in _SHELL_SURFACE_PROCS:
                out.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return out


def _foreground_hwnds(win32gui):
    """Windows to read, most-relevant first: the foreground window, the open
    Start-menu/Search overlay (if any), plus the desktop icon list (so the
    agent can launch an app from Start, search, or a desktop icon). Empty list
    if nothing usable."""
    hwnds = []
    try:
        fg = win32gui.GetForegroundWindow()
        if fg and win32gui.IsWindowVisible(fg):
            hwnds.append(fg)
    except Exception:
        pass
    # Start menu / Search overlays (see _shell_surface_hwnds) — these are NOT
    # the foreground window, so add them explicitly when open.
    for h in _shell_surface_hwnds(win32gui):
        if h not in hwnds:
            hwnds.append(h)
    # Desktop icons live under Progman > SHELLDLL_DefView > SysListView32 (or,
    # when a wallpaper slideshow is active, under a WorkerW). Always include
    # them — cheap, and essential for app-launching from a bare desktop.
    try:
        prog = win32gui.FindWindow("Progman", None)
        dv = win32gui.FindWindowEx(prog, 0, "SHELLDLL_DefView", None) if prog else 0
        if not dv:
            top = 0
            while True:
                top = win32gui.FindWindowEx(0, top, "WorkerW", None)
                if not top:
                    break
                dv = win32gui.FindWindowEx(top, 0, "SHELLDLL_DefView", None)
                if dv:
                    break
        lv = win32gui.FindWindowEx(dv, 0, "SysListView32", None) if dv else 0
        if lv and lv not in hwnds:
            hwnds.append(lv)
    except Exception:
        pass
    return hwnds


def collect_foreground_elements(*, max_elements=90, time_budget_s=1.0,
                                max_depth=25, origin_xy=(0, 0), screen_wh=None):
    """Interactive UIA elements of the foreground window (+ desktop icons) as
    (x0, y0, x1, y1, name, ctype) tuples in SCREENSHOT-pixel space (screen
    coords minus origin_xy, clipped to screen_wh). Returns [] on any failure,
    missing dep, or empty tree. Never raises.

    `max_depth` is currently unused (FindAll fetches all descendants in one
    native call); kept for signature stability / a future manual-walk fallback.
    """
    deadline = time.monotonic() + max(0.1, float(time_budget_s))
    try:
        import comtypes
        import comtypes.client
        import win32gui
    except Exception:
        return []

    # COM must be initialised on this (worker) thread. MTA avoids needing a
    # message pump. Tolerate an already-initialised apartment.
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:
        pass

    try:
        hwnds = _foreground_hwnds(win32gui)
        if not hwnds:
            return []

        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(mod.CUIAutomation,
                                           interface=mod.IUIAutomation)

        # OR-condition over the interactive control types (built once).
        type_ids = []
        for n in _INTERACTIVE_TYPE_NAMES:
            v = getattr(mod, n, None)
            if v is not None:
                type_ids.append(int(v))
        if not type_ids:
            return []
        cond = uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId,
                                           type_ids[0])
        for t in type_ids[1:]:
            cond = uia.CreateOrCondition(
                cond, uia.CreatePropertyCondition(
                    mod.UIA_ControlTypePropertyId, t))
        # ALSO mark anything that's actually clickable regardless of control
        # type — Electron/Chromium render clickable text & divs as Text/Group
        # controls that expose an Invoke (or Toggle/SelectionItem) pattern but
        # AREN'T in the control-type whitelist (e.g. Douyin's header "关注 50"
        # count is two invokable Text controls). Without this they go unmarked.
        for _pat in ("UIA_IsInvokePatternAvailablePropertyId",
                     "UIA_IsTogglePatternAvailablePropertyId",
                     "UIA_IsSelectionItemPatternAvailablePropertyId"):
            _pid = getattr(mod, _pat, None)
            if _pid is not None:
                try:
                    cond = uia.CreateOrCondition(
                        cond, uia.CreatePropertyCondition(int(_pid), True))
                except Exception:
                    pass

        ox, oy = origin_xy
        sw, sh = (screen_wh if screen_wh else (None, None))
        out = []
        seen = set()
        for hwnd in hwnds:
            if time.monotonic() > deadline or len(out) >= max_elements:
                break
            try:
                root = uia.ElementFromHandle(hwnd)
            except Exception:
                continue
            if not root:
                continue

            # On Chromium, search BOTH the page Document subtree AND the
            # whole window's chrome (tabs / bookmarks bar / address bar /
            # toolbar buttons). Earlier we restricted to Document only to
            # avoid Chrome-UI noise, but that meant individual bookmarks
            # never got marked — bookmarks bar items are MenuItem controls
            # in the browser chrome, NOT the page Document. The agent then
            # couldn't click "Gmail" in the bookmarks bar reliably. Walking
            # both subtrees is a small noise increase for big recall gain.
            # Every non-Chromium window is read whole as before.
            search_roots: list = []
            try:
                if win32gui.GetClassName(hwnd) == _CHROME_CLASS:
                    doc = root.FindFirst(
                        mod.TreeScope_Descendants,
                        uia.CreatePropertyCondition(
                            mod.UIA_ControlTypePropertyId,
                            mod.UIA_DocumentControlTypeId))
                    if doc:
                        search_roots.append(doc)
                    # ALSO walk the full window root for chrome elements
                    # (bookmarks bar, tabs, toolbar). FindAll de-dupes by
                    # the `seen` set below so any element matched by both
                    # passes appears only once.
                    search_roots.append(root)
                else:
                    search_roots.append(root)
            except Exception:
                search_roots = [root]

            arrs: list = []
            for sr in search_roots:
                try:
                    arrs.append(sr.FindAll(mod.TreeScope_Descendants, cond))
                except Exception:
                    pass
            arrs = [a for a in arrs if a]
            if not arrs:
                continue
            # Iterate through every result array in turn.
            for arr in arrs:
              n = arr.Length
              for i in range(n):
                if time.monotonic() > deadline or len(out) >= max_elements:
                    break
                try:
                    el = arr.GetElement(i)
                    if el.CurrentIsOffscreen:
                        continue
                    r = el.CurrentBoundingRectangle  # screen px
                    x0, y0 = int(r.left) - ox, int(r.top) - oy
                    x1, y1 = int(r.right) - ox, int(r.bottom) - oy
                    if x1 <= x0 or y1 <= y0:
                        continue
                    if sw is not None:
                        if x1 <= 0 or y1 <= 0 or x0 >= sw or y0 >= sh:
                            continue
                        x0 = max(0, min(x0, sw - 1)); x1 = max(1, min(x1, sw))
                        y0 = max(0, min(y0, sh - 1)); y1 = max(1, min(y1, sh))
                    key = (x0 // 8, y0 // 8, x1 // 8, y1 // 8)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        name = el.CurrentName or ""
                    except Exception:
                        name = ""
                    try:
                        ctype = int(el.CurrentControlType)
                    except Exception:
                        ctype = 0
                    out.append((x0, y0, x1, y1, str(name)[:40], str(ctype)))
                except Exception:
                    continue
        return out
    except Exception:
        return []


def _list_region_center(hint_xy, *, origin_xy, screen_wh, time_budget_s):
    """Infer a scrollable LIST/modal region from the cluster of interactive
    elements around the hint, for web lists Chrome doesn't expose with a Scroll
    pattern (a CSS-overflow modal like Douyin's follow roster). Returns a point
    ON the list (median of the clustered rows), or None when no list-like
    cluster sits around the hint. Never raises.

    Heuristic: among interactive elements (reuse collect_foreground_elements),
    keep row-sized ones whose horizontal centre is in the same column as the
    hint (within HALF_COL px); require several of them spanning real vertical
    height (a stacked list, not a lone button row). The median (x,y) of that
    cluster lands squarely on the list, so the wheel scrolls it."""
    HALF_COL = 520        # px each side of the hint x → "same column" as the list
    MIN_ROWS = 4          # need a few stacked items to call it a list
    MIN_SPAN = 120        # px of vertical extent → genuinely stacked, not a strip
    try:
        boxes = collect_foreground_elements(max_elements=160, origin_xy=origin_xy,
                                            screen_wh=screen_wh,
                                            time_budget_s=time_budget_s)
    except Exception:
        return None
    if not boxes:
        return None
    hx, hy = hint_xy
    sw = screen_wh[0] if screen_wh else None
    pts = []
    for (x0, y0, x1, y1, _name, _ct) in boxes:
        w, h = x1 - x0, y1 - y0
        if w < 60 or h < 8:                 # too small → a dot/icon, not a row
            continue
        if sw and w > 0.55 * sw:            # full-width page chrome, not a row
            continue
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        if abs(cx - hx) <= HALF_COL:        # same column as the hint
            pts.append((cx, cy))
    if len(pts) < MIN_ROWS:
        return None
    ys = sorted(p[1] for p in pts)
    if ys[-1] - ys[0] < MIN_SPAN:
        return None
    xs = sorted(p[0] for p in pts)
    return (xs[len(xs) // 2], ys[len(ys) // 2])   # median x, median y → on list


def find_scroll_target(hint_xy=None, *, origin_xy=None, screen_wh=None,
                       time_budget_s=1.0):
    """Center of the scrollable container the wheel should target, in
    SCREENSHOT-pixel space, or None on any failure (caller keeps its own
    target). Never raises.

    A mouse wheel only scrolls the element under the cursor. A web following
    list (and most modals) scrolls ONLY when the cursor sits over its
    scrollable rows — not the modal's header/padding, and not the dimmed page
    backdrop around it. The model merely *guesses* that point, so it drifts
    onto a non-scrolling zone and the wheel no-ops → the list never advances.
    This reads the OS accessibility tree for elements that actually expose a
    vertical Scroll pattern and returns a point guaranteed to be inside one,
    so the runner can aim the wheel deterministically.

    `hint_xy` (the model's intended scroll point / last click, screenshot px)
    disambiguates WHICH scrollable when several exist (a modal list sitting on
    top of the scrollable page document): the innermost Scroll-pattern container
    that CONTAINS the hint wins (the modal list, not the page behind it). When
    NOTHING with a Scroll pattern contains the hint — the common web case, where
    a modal's list is a CSS-overflow div Chrome doesn't expose as scrollable —
    we do NOT jump to a far-away scrollable (that's worse than the guess);
    instead we infer the list region from the cluster of interactive row
    elements around the hint (`_list_region_center`). With no hint at all, the
    largest scrollable (usually the page). Returned point is a CENTER — one
    stable spot, so the cursor doesn't dart around like the old multi-point
    retry. None ⇒ caller keeps its own target.
    """
    deadline = time.monotonic() + max(0.1, float(time_budget_s))
    try:
        import comtypes
        import comtypes.client
        import win32gui
    except Exception:
        return None

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:
        pass

    # Resolve screenshot-space origin/size from the primary monitor unless the
    # caller passed them (matches som.py's _uia_origin_and_size convention).
    if origin_xy is None or screen_wh is None:
        try:
            import mss
            with mss.mss() as sct:
                m = sct.monitors[1]
            if origin_xy is None:
                origin_xy = (int(m["left"]), int(m["top"]))
            if screen_wh is None:
                screen_wh = (int(m["width"]), int(m["height"]))
        except Exception:
            if origin_xy is None:
                origin_xy = (0, 0)
    ox, oy = origin_xy
    sw, sh = (screen_wh if screen_wh else (None, None))

    try:
        fg = win32gui.GetForegroundWindow()
        if not fg or not win32gui.IsWindowVisible(fg):
            return None
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(mod.CUIAutomation,
                                           interface=mod.IUIAutomation)
        root = uia.ElementFromHandle(fg)
        if not root:
            return None

        scroll_pid = getattr(mod, "UIA_IsScrollPatternAvailablePropertyId", None)
        if scroll_pid is None:
            return None
        # One native call for everything exposing a Scroll pattern.
        try:
            arr = root.FindAll(mod.TreeScope_Descendants,
                               uia.CreatePropertyCondition(int(scroll_pid), True))
        except Exception:
            arr = None

        cands = []  # (area, cx, cy, x0, y0, x1, y1)
        for i in range(arr.Length if arr else 0):
            if time.monotonic() > deadline:
                break
            try:
                el = arr.GetElement(i)
                if el.CurrentIsOffscreen:
                    continue
                # Must be VERTICALLY scrollable — skip purely-horizontal
                # scrollers (e.g. a thumbnail strip) the wheel can't drive.
                try:
                    pat = el.GetCurrentPattern(mod.UIA_ScrollPatternId)
                    sp = pat.QueryInterface(mod.IUIAutomationScrollPattern)
                    if not sp.CurrentVerticallyScrollable:
                        continue
                except Exception:
                    continue
                r = el.CurrentBoundingRectangle  # screen px
                x0, y0 = int(r.left) - ox, int(r.top) - oy
                x1, y1 = int(r.right) - ox, int(r.bottom) - oy
                if x1 <= x0 or y1 <= y0:
                    continue
                if sw is not None:
                    if x1 <= 0 or y1 <= 0 or x0 >= sw or y0 >= sh:
                        continue
                    x0 = max(0, min(x0, sw - 1)); x1 = max(1, min(x1, sw))
                    y0 = max(0, min(y0, sh - 1)); y1 = max(1, min(y1, sh))
                cands.append(((x1 - x0) * (y1 - y0),
                              (x0 + x1) // 2, (y0 + y1) // 2,
                              x0, y0, x1, y1))
            except Exception:
                continue

        if hint_xy is not None:
            hx, hy = hint_xy
            # Pass 1: a real Scroll-pattern container that CONTAINS the hint —
            # the innermost wins (the modal list, not the page document behind
            # it). EXCLUDE the page document itself: Chrome 138+ exposes the
            # top-level renderer as Scroll-pattern and its bounds cover the
            # whole viewport, so it ALSO contains the hint and used to win when
            # nothing smaller was tagged. On a centred web modal (Douyin's
            # follow roster, etc.) that left the wheel landing on the page
            # document BEHIND the modal — scroll dispatched but the modal
            # didn't move because the page document was its own scroll target.
            # We treat anything whose area is >= 60 % of the screen as the
            # page document and skip it; Pass 2 then runs to infer the modal
            # list from the cluster of interactive ROW elements around the hint.
            page_area_threshold = None
            if sw is not None and sh is not None:
                page_area_threshold = 0.60 * sw * sh
            containing = [c for c in cands
                          if c[3] <= hx <= c[5] and c[4] <= hy <= c[6]
                          and (page_area_threshold is None
                               or c[0] < page_area_threshold)]
            if containing:
                c = min(containing, key=lambda c: c[0])  # innermost
                return (c[1], c[2])
            # Pass 2: many web lists/modals (e.g. Douyin's follow roster) scroll
            # via a CSS-overflow div that Chrome does NOT expose with a Scroll
            # pattern, so Pass 1 finds nothing around the hint. Do NOT fall back
            # to a far-away scrollable (e.g. the left nav sidebar) — that's
            # strictly WORSE than the model's own guess. Instead infer the list
            # region from the cluster of interactive ROW elements around the
            # hint (those ARE in the tree) and scroll its center.
            remaining = max(0.15, deadline - time.monotonic())
            return _list_region_center(hint_xy, origin_xy=(ox, oy),
                                       screen_wh=screen_wh,
                                       time_budget_s=remaining)
        # No hint: the largest scrollable (usually the page), if any.
        if cands:
            c = max(cands, key=lambda c: c[0])
            return (c[1], c[2])
        return None
    except Exception:
        return None


# Backwards-compatible alias (older callers / docs referenced the Chrome name).
collect_chrome_elements = collect_foreground_elements
