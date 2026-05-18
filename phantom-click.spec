# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — packages phantom-click as a macOS .app bundle.

Run via `./build-mac.sh` (which injects build_config first). Don't invoke
pyinstaller on this directly without the build script — the bundle will
ship with placeholder bridge URL / token and refuse to function.
"""
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()

block_cipher = None

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
        # Quartz / AppKit primitives used by phantom.py (input + screen).
        'Quartz',
        'AppKit',
        'objc',
    ],
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='phantom-click',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    target_arch=None,       # universal2 would double size; arm64 is fine
    codesign_identity='-',  # ad-hoc — real signing happens in build-mac.sh
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='phantom-click',
)

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
        # The text shows up in the permission prompt — keep it short and
        # in Chinese for the target audience.
        'NSScreenCaptureUsageDescription':
            '噜噜机器人需要"屏幕录制"权限来截取屏幕并理解界面，然后替你完成任务。',
        # Apple Events (AppleScript) for window focus management.
        'NSAppleEventsUsageDescription':
            '噜噜机器人需要权限来切换前台窗口，以便它能操控你指定的应用。',
        # Sending mouse/keyboard events triggers macOS Accessibility prompt.
        # That permission has no Info.plist key — the user grants it via
        # System Settings → Privacy & Security → Accessibility on first
        # input event. We just need a clear bundle name (above) to identify
        # this app in that list.
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        # No automatic dark mode flip — UI is dark-themed by design.
        'NSRequiresAquaSystemAppearance': False,
        # Background category — app pretends to be foreground (has a window)
        # but we don't want a dock-shake on launch.
        'LSApplicationCategoryType': 'public.app-category.productivity',
    },
)
