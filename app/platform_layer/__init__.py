"""Single seam between the rest of the app and OS-specific code.

Everything cross-platform-conditional lives here:
  • the right Input/Screen implementations for this host,
  • the typed `capabilities` object exposing platform facts the runner /
    vision / som modules need to vary by OS (does this host expose a UIA
    a11y tree? where is the menu bar? what's the select-all combo?).

ENFORCED INVARIANT: no other module in `app/` may import `sys.platform`
to branch on the OS — they import `capabilities` from here instead. If
you find yourself adding a new `if sys.platform == "darwin":` somewhere
outside this package, stop and add a field to `Capabilities` instead.
This is why the previous regression happened: a y<25 macOS-menu-bar
check landed in app/runner.py with no platform gate at all and fired on
Windows where the title bar / address bar legitimately live below y=25.
Routing every such question through `capabilities` makes that class of
bug structurally impossible to add again.
"""
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    """Platform facts the rest of the app needs to vary on. Adding a new
    field means changing exactly two places: the field definition here, and
    the two literal instances below. No `sys.platform` branching anywhere
    else in the app."""

    # Display name used in the system prompt ("Windows" / "macOS"). Drives
    # how the model is introduced to the host OS, not control flow.
    name: str

    # Primary modifier on this OS (for keyboard shortcuts the model is told
    # to use): "ctrl" on Windows, "cmd" on macOS.
    primary_mod: str

    # Does this host expose a UI Automation accessibility tree we can read
    # for element grounding and scroll-target snapping? Windows: yes
    # (`uiautomation` lib over comtypes). macOS: not wired up — there IS an
    # AX equivalent but app/uia_win.py is Windows-only and `find_scroll_target`
    # short-circuits to None off-Windows.
    has_uia: bool

    # Pixels at the TOP of the screen reserved for the OS's global menu bar.
    # On macOS the menu bar is a system-wide strip; a click with y<25 lands
    # there and opens a system dropdown that derails the agent. On Windows
    # there IS no global menu bar — those same y<25 pixels are legitimate
    # click targets (Chrome's title bar / address bar / close button), so the
    # rejection MUST be a no-op. Setting this to 0 on Windows makes the
    # standard `if y < capabilities.menu_bar_max_y: reject` check inert.
    menu_bar_max_y: int

    # Should the SYSTEM_PROMPT include the "after opening a window, click
    # its maximize button" rule? Windows: yes (a non-maximized Chrome leaves
    # the taskbar + other windows visible, which derails SoM detection).
    # macOS: no — the equivalent (green button) enters fullscreen which
    # HIDES the menu bar and is worse for the agent.
    prompt_window_maximize: bool

    # Select-all combo, e.g. for clearing a text field before re-typing.
    # Differs purely by primary modifier.
    select_all_combo: str


if sys.platform == "darwin":
    from .mac import Input, Screen
    capabilities = Capabilities(
        name="macOS",
        primary_mod="cmd",
        has_uia=False,
        menu_bar_max_y=25,
        prompt_window_maximize=False,
        select_all_combo="cmd+a",
    )
elif sys.platform == "win32":
    from .win import Input, Screen
    capabilities = Capabilities(
        name="Windows",
        primary_mod="ctrl",
        has_uia=True,
        menu_bar_max_y=0,
        prompt_window_maximize=True,
        select_all_combo="ctrl+a",
    )
else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")

__all__ = ["Input", "Screen", "Capabilities", "capabilities"]
