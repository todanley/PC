# Windows Handoff

The macOS side of phantom-click is fully working and shipped. The Windows
`.exe` built by CI **does not work** on a real Windows machine (user-confirmed
2026-05-19). This document is everything a Claude Code instance running on
Windows needs to pick up the work.

Repo: `https://github.com/todanley/PC` · branch `main` · latest tag at HEAD.

## 1. What this app is

Desktop app that drives the user's computer to complete one-line natural-
language tasks. PySide6 GUI; the user types "打开计算器算 17×24", hits 运行,
the app screenshots the screen, asks Gemini what to do next, dispatches the
returned action (click / type / key / scroll / drag), loops until Gemini says
`done`.

Target audience for THIS distribution: mainland-China users who can't reach
Gemini directly. We bake a public bridge URL + shared bearer token into the
binary so they "just open and run" — no env vars, no settings UI.

## 2. Request flow (don't break this)

```
CN user's machine
    └── phantom-click.exe
            │  POST /v1beta/openai/chat/completions
            │  Authorization: Bearer pc_sV2X… (baked into the binary)
            │  User-Agent: phantom-click/0.1.0  (must be set or CF Bot Fight Mode 403s)
            ▼
        https://bridge.z1nexusn1.org/*
            │  Cloudflare Worker — phantom-click-bridge
            │  Verifies bearer against D1 (phantom-click-tokens)
            │  Atomic UPDATE bumps daily counter, enforces max_calls_per_day
            │  Re-signs Authorization header with GEMINI_API_KEY secret
            ▼
        https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
            │  Standard OpenAI-compat shape, returns action JSON
            ▼
        back through the Worker, back to the .exe, action dispatched
```

The Worker is already deployed, healthy, and verified from CN
(17ce.com 156 nodes green, greatfire.org 0% blocked). **DO NOT touch the
Worker or Cloudflare-side config** while fixing Windows — both build targets
share the same backend.

## 3. Repo layout — Windows-relevant files

```
phantom-click/
├── app/
│   ├── main.py                  Qt entry. _install_clean_shutdown wires
│   │                            aboutToQuit / closeEvent so the TaskRunner
│   │                            thread is cancelled before QApplication is
│   │                            destroyed (otherwise SIGABRT on Cmd+Q).
│   ├── ui.py                    MainWindow. closeEvent handler does the
│   │                            same cancel-before-destroy dance.
│   ├── runner.py                TaskRunner(QThread). The agent loop:
│   │                            screenshot -> vision -> dispatch action.
│   ├── vision.py                The bridge call. Reads build_config.
│   │                            Sets User-Agent header explicitly to avoid
│   │                            Cloudflare Bot Fight Mode 403s.
│   ├── build_config.py          Build-time injection. BRIDGE_URL and
│   │                            BRIDGE_TOKEN literals get sed-replaced by
│   │                            build-win.ps1 / build-mac.sh before
│   │                            PyInstaller runs. If env vars are set, they
│   │                            override (dev mode).
│   ├── knowledge.md             Bundled at runtime via PyInstaller datas=.
│   └── platform_layer/
│       ├── __init__.py          Dispatches to mac.py / win.py via sys.platform.
│       ├── mac.py               Quartz CGEvent input + screencapture.
│       └── win.py               pyautogui + mss. *** This is the file most
│                                likely to have a bug on real Windows. ***
├── phantom-click.spec           PyInstaller spec. Conditional on
│                                sys.platform: BUNDLE() runs only on macOS;
│                                hiddenimports swaps Quartz/AppKit/objc for
│                                pyautogui/mss/win32* on Windows.
├── phantom_click_main.py        Entry point wrapper (sidesteps PyInstaller's
│                                "no parent package" issue with relative imports).
├── build-win.ps1                PowerShell build script: bakes config,
│                                runs PyInstaller, zips dist\噜噜机器人-win.zip.
├── requirements-app.txt         Has sys_platform markers — pyobjc-* on darwin,
│                                pywin32 on win32. numpy is also needed.
├── .github/workflows/build.yml  Matrix build (macos-14 + windows-latest).
│                                Bakes secrets from repo settings, uploads
│                                artifacts as phantom-click-{mac,win}.
└── docs/WINDOWS_HANDOFF.md      This file.
```

NOT relevant for Windows work (skip unless something else is going wrong):
`bridge/` (retired Mac-side FastAPI bridge), `worker/` (deployed CF Worker,
already live), `build-mac.sh`, `phantom.py` (Mac-only Quartz primitives).

## 4. What's verified working

- **macOS side**: builds via `./build-mac.sh`, launches, runs tasks, exits
  cleanly on Cmd+Q without SIGABRT, ships as `dist/噜噜机器人-mac.zip`.
- **CI Mac build**: GitHub Actions `macos-14` job produces an identical
  artifact, user confirmed it works after download.
- **Worker + D1 + tokens**: 100+ successful Gemini calls forwarded; daily
  quota counters atomic; CN-reachable.
- **Bridge URL + token are correct**: baked into both Mac and Windows
  binaries identically. If the Windows .exe ever DOES reach the bridge,
  authentication will succeed.

## 5. What's broken

**The Windows `.exe` doesn't work.** User-reported "doesn't work" on a real
Windows machine after downloading the CI artifact `phantom-click-win`.
Specific failure mode not yet captured — that's the first thing to find out.

## 6. First diagnostic step (DO THIS FIRST)

Get a precise reproduction. Build the bundle locally on the Windows machine,
launch from PowerShell (NOT Explorer double-click — we need stderr):

```powershell
# Clone
git clone https://github.com/todanley/PC.git
cd PC

# Deps
python -m pip install -r requirements-app.txt
python -m pip install pyinstaller

# Bake config + build
$env:PHANTOM_BRIDGE_URL = "https://bridge.z1nexusn1.org"
$env:PHANTOM_BRIDGE_TOKEN = "pc_sV2XYmHT1i1k8qiB53Ohv7dSJx1U8xep"
.\build-win.ps1

# Run from PowerShell so stderr / Python tracebacks are visible
.\dist\噜噜机器人\phantom-click.exe
```

What to look for:

- **Does the window appear at all?** If not → PyInstaller bundling issue
  (missing DLL, missing hiddenimport, Qt platform plugin not found).
- **Does it appear, but Run does nothing?** → probably an exception in
  `app/vision.py` (bridge call) or `app/platform_layer/win.py` (screenshot
  / input). Catch the traceback from PowerShell stderr.
- **Does it appear, Run works, but actions don't land on right targets?**
  → high-DPI coordinate scaling. Windows ≥ 8.1 has per-monitor DPI; mss
  may return logical-px screenshots while pyautogui clicks in physical-px,
  or vice versa.

Also useful, to get a console window during runtime (PyInstaller `console=False`
suppresses it):

```powershell
# Temporarily flip the spec to keep a console for debugging:
# In phantom-click.spec, change `console=False,` to `console=True,` then
# rebuild. The .exe will open a CMD window alongside the GUI showing all
# stdout/stderr in real time. Revert before shipping.
```

## 7. Suspect failure modes (ranked by probability)

1. **Qt platform plugin not bundled** — PySide6's `qwindows.dll` must be
   present in `dist\噜噜机器人\PySide6\plugins\platforms\`. If missing, the
   app exits with `qt.qpa.plugin: Could not find the Qt platform plugin
   "windows"`. Fix: PyInstaller's PySide6 hook usually handles this; if not,
   add `--collect-all PySide6` to the spec or COLLECT(...) call.

2. **Missing hiddenimport** — phantom-click.spec includes the obvious
   `pyautogui`, `mss`, `win32api/con/gui` for Windows. pyautogui transitively
   depends on `pyscreeze`, `pymsgbox`, `pytweening`, `mouseinfo` — those
   may need to be added explicitly if PyInstaller's analyser misses them.
   Symptom: ImportError in PowerShell stderr.

3. **Chinese folder name `噜噜机器人` confusing some Windows code path** —
   if the user's system locale is non-Unicode, the folder name may render
   garbled and tools that don't handle UTF-8 paths fail. Worth testing
   by renaming the folder to ASCII (e.g. `phantom-click`) and seeing if
   that fixes it. If yes, output the COLLECT folder under an ASCII name on
   Windows (see `phantom-click.spec` — already does `name='噜噜机器人' if
   IS_WIN else 'phantom-click'`; might need to flip that).

4. **High-DPI coordinate mismatch** — both screenshot and click must be in
   the same coordinate space. On Windows 10/11 with non-100% display scale:
   - mss returns physical pixels by default
   - pyautogui clicks in logical pixels by default (DPI-aware)
   - vision.py's `_encode_image` auto-detects image vs logical size and
     sets `self.scale` accordingly — this SHOULD just work, but verify
     by checking what `screen.size()` returns vs the actual screencap
     dimensions.
   - Quick fix if needed: `app.platform_layer.win.Screen.size()` should
     return logical pixels (matches pyautogui's coord space). Currently
     it uses `mss.monitors[1]['width']` which may be physical px on
     high-DPI displays.

5. **SmartScreen `Windows protected your PC`** — not a crash, but blocks
   first launch via Explorer. User must click "More info" → "Run anyway".
   If this is the actual "doesn't work" — write it up in a README the
   .zip ships with. To verify, launch from PowerShell instead; SmartScreen
   doesn't gate that path.

6. **pywin32 post-install** — pywin32 normally needs `python -m pywin32_postinstall`
   to register its COM objects. In a PyInstaller bundle it's frozen at
   build time, so this usually doesn't matter at runtime. If you see
   `ImportError: DLL load failed` for pywin32 modules in the bundle, the
   post-install wasn't run in CI before PyInstaller ran.

7. **The build-win.ps1 PowerShell script may have a bug** — never tested
   on a real Windows machine, only validated as syntactically-PowerShell.
   Run it line-by-line if it errors.

## 8. The bridge / Worker side (probably untouched, but for context)

- **URL**: `https://bridge.z1nexusn1.org` (Cloudflare named tunnel
  + Worker route; Worker takes precedence over the CNAME)
- **Shared CN-pool token**: `pc_sV2XYmHT1i1k8qiB53Ohv7dSJx1U8xep`
  (5000 calls/day cap, label `public-pool`). Already baked into shipped
  binaries; safe to reuse for testing.
- **Health check**: `curl https://bridge.z1nexusn1.org/healthz` returns
  `{"ok":true,"worker":true}`. If this works from Windows, then the bridge
  is reachable and any failure is local to the Windows binary.
- **Live logs**: from a machine with `wrangler` auth (the Mac side, not
  Windows), run `cd worker && npx wrangler tail`. Useful when reproducing
  the failure — you'll see whether requests are arriving at the Worker or
  the binary is failing before that.

## 9. Iteration loop

```powershell
# Edit code locally on Windows
# Rebuild
.\build-win.ps1
# Run
.\dist\噜噜机器人\phantom-click.exe
# When it works, commit + push
git add . ; git commit -m "fix(win): <what>" ; git push
# CI rebuilds both platforms on push; download artifacts to verify
```

To trigger CI manually without pushing:
```
gh workflow run build.yml --repo todanley/PC --ref main
gh run watch --repo todanley/PC --exit-status
gh run download --repo todanley/PC --name phantom-click-win
```

## 10. Where the GEMINI_API_KEY lives (so you don't accidentally leak it)

- **On Cloudflare**, as a Worker secret. Encrypted at rest, never visible
  in code, logs, or git.
- **NOT** in this repo, NOT in the .exe, NOT in the bridge token, NOT in CI.
- If you somehow need to rotate it: from the Mac side, `cd worker && npx
  wrangler secret put GEMINI_API_KEY`. CN users' binaries keep working
  unchanged.

## 11. Status checklist for "Windows .exe ready to ship"

- [ ] `.\dist\噜噜机器人\phantom-click.exe` launches and shows the GUI window
- [ ] Typing a task in the input box and clicking 运行 makes a successful
      call to the Worker (verified via `wrangler tail` showing
      `event:"forward"` for `public-pool`)
- [ ] Click coordinates land on the right targets at 100% display scale
- [ ] Click coordinates land on the right targets at 150% / 200% display scale
- [ ] Window closes cleanly via the red X button (no Windows Error Reporting
      crash dialog)
- [ ] No new crash entries in Windows Event Viewer → Windows Logs →
      Application after the run + close cycle
- [ ] Cold-run of the CI-built `dist/噜噜机器人-win.zip` after extracting
      to e.g. `Desktop\噜噜机器人\` works the same as a locally-built run
- [ ] User can complete one real task (e.g. "打开计算器算 17×24") with the
      shipped zip on a clean Windows machine they don't usually develop on

When all of those are checked, push a tag (e.g. `v0.1.0-win`) and the
GitHub Actions workflow attaches both Mac + Win zips to a release the
user can hand to CN testers.
