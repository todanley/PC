"""Windows UI Automation reader for Set-of-Mark.

Reads interactive elements of the focused Chrome window from the OS
accessibility tree (UI Automation) and returns their screen-space bounding
boxes. This is DETECTION-FREE: it reads the a11y tree at the OS level, invisible
to the page's JavaScript, and injects nothing into the page (clicks still go
through real OS mouse events elsewhere). Used by app/som.py `_detect_uia` to
mark small/semantic controls — e.g. a tiny "···" more-button — that OCR and the
icon contour pass miss.

Windows-only. All COM/comtypes work is isolated here and imported lazily, so
importing this module is cheap and it degrades to [] anywhere it can't run.
"""
import time

# Chrome's top-level window class (matches app/platform_layer/win.py).
_CHROME_CLASS = "Chrome_WidgetWin_1"

# UIA ControlType names we consider "interactive" / worth marking. Resolved to
# their integer IDs from the generated comtypes module at call time.
_INTERACTIVE_TYPE_NAMES = (
    "UIA_ButtonControlTypeId", "UIA_HyperlinkControlTypeId",
    "UIA_EditControlTypeId", "UIA_MenuItemControlTypeId",
    "UIA_CheckBoxControlTypeId", "UIA_RadioButtonControlTypeId",
    "UIA_ListItemControlTypeId", "UIA_TabItemControlTypeId",
    "UIA_ComboBoxControlTypeId", "UIA_SplitButtonControlTypeId",
    "UIA_MenuControlTypeId", "UIA_ImageControlTypeId",
)


def _focused_chrome_hwnd(win32gui):
    """HWND of the focused Chrome window, else the first visible Chrome
    top-level, else 0."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd and win32gui.GetClassName(hwnd) == _CHROME_CLASS:
            return hwnd
    except Exception:
        pass
    found = []

    def _cb(h, _):
        try:
            if (win32gui.IsWindowVisible(h)
                    and win32gui.GetClassName(h) == _CHROME_CLASS
                    and win32gui.GetWindowText(h)):
                found.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return 0
    return found[0] if found else 0


def collect_chrome_elements(*, max_elements=80, time_budget_s=1.0,
                            max_depth=25, origin_xy=(0, 0), screen_wh=None):
    """Interactive UIA elements of the focused Chrome window as
    (x0, y0, x1, y1, name, ctype) tuples in SCREENSHOT-pixel space (screen
    coords minus origin_xy, clipped to screen_wh). Returns [] on any failure,
    missing dep, non-Chrome foreground, or empty tree. Never raises.

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
        hwnd = _focused_chrome_hwnd(win32gui)
        if not hwnd:
            return []

        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(mod.CUIAutomation,
                                           interface=mod.IUIAutomation)
        win = uia.ElementFromHandle(hwnd)
        if not win:
            return []

        # Prefer the page Document subtree so we mark page content, not the
        # browser chrome (tabs/toolbar). Fall back to the whole window.
        search_root = win
        try:
            doc_cond = uia.CreatePropertyCondition(
                mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId)
            doc = win.FindFirst(mod.TreeScope_Descendants, doc_cond)
            if doc:
                search_root = doc
        except Exception:
            pass

        # OR-condition over the interactive control types.
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

        arr = search_root.FindAll(mod.TreeScope_Descendants, cond)
        if not arr:
            return []
        n = arr.Length

        ox, oy = origin_xy
        sw, sh = (screen_wh if screen_wh else (None, None))
        out = []
        for i in range(n):
            if time.monotonic() > deadline or len(out) >= max_elements:
                break
            try:
                el = arr.GetElement(i)
                if el.CurrentIsOffscreen:
                    continue
                r = el.CurrentBoundingRectangle  # screen px: left/top/right/bottom
                x0, y0 = int(r.left) - ox, int(r.top) - oy
                x1, y1 = int(r.right) - ox, int(r.bottom) - oy
                if x1 <= x0 or y1 <= y0:
                    continue
                if sw is not None:
                    if x1 <= 0 or y1 <= 0 or x0 >= sw or y0 >= sh:
                        continue
                    x0 = max(0, min(x0, sw - 1)); x1 = max(1, min(x1, sw))
                    y0 = max(0, min(y0, sh - 1)); y1 = max(1, min(y1, sh))
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
