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


def _foreground_hwnds(win32gui):
    """Windows to read, most-relevant first: the foreground window, plus the
    desktop icon list (so the agent can launch an app by double-clicking its
    desktop icon). Empty list if nothing usable."""
    hwnds = []
    try:
        fg = win32gui.GetForegroundWindow()
        if fg and win32gui.IsWindowVisible(fg):
            hwnds.append(fg)
    except Exception:
        pass
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

            # Chromium only: prefer the page Document subtree so we mark page
            # content, not the browser chrome. Every other window is read whole.
            search_root = root
            try:
                if win32gui.GetClassName(hwnd) == _CHROME_CLASS:
                    doc = root.FindFirst(
                        mod.TreeScope_Descendants,
                        uia.CreatePropertyCondition(
                            mod.UIA_ControlTypePropertyId,
                            mod.UIA_DocumentControlTypeId))
                    if doc:
                        search_root = doc
            except Exception:
                pass

            try:
                arr = search_root.FindAll(mod.TreeScope_Descendants, cond)
            except Exception:
                continue
            if not arr:
                continue
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


# Backwards-compatible alias (older callers / docs referenced the Chrome name).
collect_chrome_elements = collect_foreground_elements
