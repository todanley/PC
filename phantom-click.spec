# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — cross-platform packaging for phantom-click.

Run via `./build-mac.sh` on macOS or `./build-win.ps1` on Windows (both inject
build_config first). Don't invoke pyinstaller on this directly without the
build script — the resulting binary ships with placeholder bridge URL / token
and refuses to function.
"""
import os
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
ROOT = Path(SPECPATH).resolve()

block_cipher = None

# Platform-specific hidden imports. Quartz/AppKit only exist on macOS; the
# Windows side relies on pyautogui + mss + pywin32, which PyInstaller's
# default analyser usually finds without hints.
PLAT_HIDDEN = ['Quartz', 'AppKit', 'objc'] if IS_MAC else [
    'pyautogui', 'mss', 'mss.tools', 'pyperclip',
    # pywin32 sub-modules pyautogui touches on Windows.
    'win32api', 'win32con', 'win32gui',
]

a = Analysis(
    ['phantom_click_main.py'],
    pathex=[str(ROOT)],
    binaries=[],
    # knowledge.md is read at runtime by VisionClient — bundle it next to the
    # `app` package inside the .app's Frameworks dir.
    datas=[
        ('app/knowledge.md', 'app'),
    ],
    hiddenimports=[
        # PySide6 sub-modules PyInstaller's analyser sometimes misses.
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ] + PLAT_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PIL/Pillow's tkinter integration is huge and we don't use it.
        'tkinter',
        # Test machinery that gets pulled in transitively.
        'pytest', 'unittest',
        # Network libs only the bridge server uses.
        'fastapi', 'uvicorn', 'httpx',
        # Transitively pulled in by something; our Python install only has
        # an x86_64 wheel of _yaml.so which breaks arm64 packaging.
        'yaml', '_yaml',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_kwargs = dict(
    exclude_binaries=True,
    name='phantom-click',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no terminal window on either platform
    disable_windowed_traceback=False,
)
if IS_MAC:
    exe_kwargs.update(
        target_arch=None,       # universal2 would double size; arm64 is fine
        codesign_identity='-',  # ad-hoc — real signing happens in build-mac.sh
        entitlements_file=None,
    )

exe = EXE(pyz, a.scripts, [], **exe_kwargs)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    # Folder name PyInstaller emits inside dist/. On Windows this folder IS
    # the deliverable; on macOS it gets wrapped by BUNDLE() below into an
    # .app, and the folder name doesn't matter to the user.
    name='噜噜机器人' if IS_WIN else 'phantom-click',
)

# BUNDLE() is macOS-only — wrapping a COLLECT into an .app makes no sense on
# Windows (where the deliverable is a folder containing the .exe). Guard it.
if IS_MAC:
    app = BUNDLE(
        coll,
        name='噜噜机器人.app',
        icon=None,
        bundle_identifier='com.phantomclick.lulu',
        info_plist={
            'CFBundleName': '噜噜机器人',
            'CFBundleDisplayName': '噜噜机器人',
            'CFBundleShortVersionString': '0.1.0',
            'CFBundleVersion': '0.1.0',
            # Required for macOS to grant Screen Recording on first capture.
            'NSScreenCaptureUsageDescription':
                '噜噜机器人需要"屏幕录制"权限来截取屏幕并理解界面，然后替你完成任务。',
            # Apple Events (AppleScript) for window focus management.
            'NSAppleEventsUsageDescription':
                '噜噜机器人需要权限来切换前台窗口，以便它能操控你指定的应用。',
            # Sending mouse/keyboard events triggers macOS Accessibility prompt.
            # That permission has no Info.plist key — the user grants it via
            # System Settings → Privacy & Security → Accessibility on first
            # input event. The bundle name (above) identifies the app there.
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            # No automatic dark mode flip — UI is dark-themed by design.
            'NSRequiresAquaSystemAppearance': False,
            'LSApplicationCategoryType': 'public.app-category.productivity',
        },
    )
