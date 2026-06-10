# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — cross-platform packaging for LuLuBot.

Run via `./build-mac.sh` on macOS or `./build-win.ps1` on Windows (both inject
build_config first). Don't invoke pyinstaller on this directly without the
build script — the resulting binary ships with placeholder bridge URL / token
and refuses to function.
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
ROOT = Path(SPECPATH).resolve()

block_cipher = None

# Set-of-Mark OCR (optional, PHANTOM_SOM=1). RapidOCR keeps its PP-OCR .onnx
# models + config.yaml as package data, and onnxruntime loads native DLLs via
# dynamic dispatch the analyser can't trace — so collect_all() for both,
# pulling datas + binaries + hiddenimports. Guarded so a checkout without the
# OCR deps installed still builds (SoM just stays unavailable at runtime).
SOM_DATAS, SOM_BINARIES, SOM_HIDDEN = [], [], []
_SOM_PKGS = ["rapidocr_onnxruntime", "onnxruntime"]
# comtypes (Windows only): backs the UIA element source (PHANTOM_SOM_UIA,
# app/uia_win.py). collect_all so the COM type-lib generation machinery ships;
# comtypes writes its generated wrappers to a temp dir when frozen.
if IS_WIN:
    _SOM_PKGS.append("comtypes")
for _pkg in _SOM_PKGS:
    try:
        _d, _b, _h = collect_all(_pkg)
        SOM_DATAS += _d
        SOM_BINARIES += _b
        SOM_HIDDEN += _h
    except Exception:
        pass

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
    binaries=[] + SOM_BINARIES,
    # knowledge.md is read at runtime by VisionClient — bundle it next to the
    # `app` package inside the .app's Frameworks dir.
    datas=[
        ('app/knowledge.md', 'app'),
    ] + SOM_DATAS,
    hiddenimports=[
        # PySide6 sub-modules PyInstaller's analyser sometimes misses.
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ] + PLAT_HIDDEN + SOM_HIDDEN,
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
    ] + (
        # macOS only: our Python install ships an x86_64-only _yaml.so that
        # breaks arm64 packaging. On Windows we KEEP yaml — RapidOCR (SoM
        # OCR) imports it to read its model config.yaml; excluding it makes
        # the OCR engine fail to load in the bundle.
        ['yaml', '_yaml'] if IS_MAC else []
    ),
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if IS_WIN:
    # Onefile: single self-contained .exe in dist/. PyInstaller's bootloader
    # self-extracts to %TEMP%\_MEI<rand> on launch. Slower cold-start than
    # onedir but ships as one file the user can double-click.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='LuLuBot',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
    )
else:
    # macOS: onedir → COLLECT → BUNDLE into .app
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='LuLuBot',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity='-',
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
        name='LuLuBot',
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
