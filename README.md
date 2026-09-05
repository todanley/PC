# LuLuBot 噜噜机器人

**A semi-computer-use desktop automation agent powered by AI vision.**

LuLuBot watches your screen, decides one action at a time with a vision-capable LLM, and drives your mouse & keyboard through OS-level input APIs. It works across native apps, browsers, games, and legacy software — anything with pixels on your display.

Bring your own API key from Anthropic, Google, or Moonshot; nothing runs through a hosted backend.

---

## Table of contents

- [What "semi-computer-use" means](#what-semi-computer-use-means)
- [How it works](#how-it-works)
- [Feature highlights](#feature-highlights)
- [Supported AI providers](#supported-ai-providers)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running a task](#running-a-task)
- [The action loop in detail](#the-action-loop-in-detail)
- [Configuration reference](#configuration-reference)
- [Windows platform improvements](#windows-platform-improvements)
- [Baseline regression suite](#baseline-regression-suite)
- [OS permissions](#os-permissions)
- [Building a standalone binary](#building-a-standalone-binary)
- [Project structure](#project-structure)
- [Knowledge base (`app/knowledge.md`)](#knowledge-base-appknowledgemd)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Safety notes](#safety-notes)
- [Contributing](#contributing)
- [License](#license)

---

## What "semi-computer-use" means

Full "computer-use" agents (e.g. Anthropic's `computer_20250124` tool, browser-use, playwright-based agents) often combine vision with DOM/accessibility trees, `set-of-mark` overlays, or browser-extension hooks to help the model target elements.

LuLuBot deliberately does **not**. Each turn is:

1. **Capture** — one raw screenshot of the display.
2. **Ask** — send the screenshot + task + history to a vision LLM.
3. **Act** — execute the single JSON action returned, via native OS input.
4. **Loop** — re-screenshot and re-decide from scratch.

There is no cached DOM, no accessibility tree, no persistent element registry. Every decision is grounded in the pixels visible right now. That trade-off:

- ✅ Works across native apps, games, remote desktops, PDFs, anything on screen.
- ✅ Immune to DOM churn or accessibility-tree quirks.
- ✅ Provider-agnostic — swap Claude for Gemini or Kimi in one env var.
- ⚠️ Slower than DOM-augmented agents (one round-trip per action).
- ⚠️ More sensitive to visual clutter, small targets, and hidden state.

An opt-in **Set-of-Mark** mode (`PHANTOM_SOM=1`) overlays OCR text boxes + icon contours for cases where raw pixels aren't enough. See [`app/som.py`](app/som.py).

---

## How it works

```
┌──────────────────────────────────────────────────────────┐
│                     Your screen                          │
└──────────────────┬───────────────────────────────────────┘
                   │ screenshot (JPEG, ~75% quality)
                   ▼
          ┌──────────────────┐
          │  AI vision model │  ← Claude / Gemini / Kimi
          │  (your API key)  │
          └────────┬─────────┘
                   │ one JSON action
                   ▼
       click / type / scroll / drag / key / done
                   │
                   ▼
         native OS input (CGEvent on macOS,
                          SendInput on Windows)
                   │
                   ▼
         next screenshot → verify → repeat
```

The runner (`app/runner.py`) also:

- **Verifies each action** with a pixel-diff around the click site to catch no-ops (button didn't actually toggle, scroll landed on inert region).
- **Optionally records** the whole session to `<rundir>/screen.mp4` via a bundled ffmpeg binary.
- **Humanizes input** — jittered mouse paths, per-character type delays — to avoid triggering trivial bot detectors (`app/humanize.py`).
- **Rate-limits** actions/minute and tasks/hour if you configure it, so you don't accidentally burn API credit in a runaway loop.

---

## Feature highlights

- **Multi-provider vision**: Anthropic Claude, Google Gemini (API or logged-in browser), Moonshot Kimi.
- **Cross-platform**: macOS 13+ and Windows 10/11. Same UI, same task language.
- **Local-first**: no server, no telemetry, no account. API keys live in your OS config folder or environment.
- **PySide6 desktop UI**: run/stop, live action log, screenshot preview, settings dialog.
- **Screen recording** of every run for post-mortem (opt-out via `PHANTOM_RECORD=0`).
- **Set-of-Mark mode** (opt-in) for pixel-hostile UIs — OCR + icon contour overlays.
- **Windows UI Automation source** (opt-in) — pulls accessibility labels for even richer marks.
- **Knowledge base injection** — `app/knowledge.md` is appended to the system prompt every turn, so app-specific quirks (e.g. Douyin's follow-count location) are learned once and reused forever.
- **PyInstaller packaging** — ships as `LuLuBot.app` / `LuLuBot.exe`.

---

## Supported AI providers

| Provider | Model examples | Key env var | Notes |
|---|---|---|---|
| **Anthropic (Claude)** | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5` | `ANTHROPIC_API_KEY` | Default. Best accuracy, highest cost. |
| **Google AI Studio (Gemini)** | `gemini-2.5-flash`, `gemini-2.5-flash-lite` | `GEMINI_API_KEY` | Fastest turn (~3–5 s), cheap. Set `PHANTOM_PROVIDER=google`. |
| **Moonshot (Kimi)** | `kimi-k2.5`, `kimi-k2.6` | `ANTHROPIC_API_KEY` (reused) | ~25× cheaper than Claude Opus per input token. OpenAI-compatible. |
| **Gemini via browser** | any `gemini-*` | none | Drives `gemini.google.com` via Playwright using your logged-in Chrome cookies. Free under a Gemini Pro subscription. Slower (~30–60 s/turn) and a Chromium window pops up. Requires the CLI at `~/.claude/tools/gemini.py` — see `PHANTOM_GEMINI_CLI`. |

Provider is inferred from the model prefix (`claude-*` → anthropic, `kimi-*` → moonshot, `gemini-*` → google if `GEMINI_API_KEY` set, else browser). Force it explicitly with `PHANTOM_PROVIDER`.

---

## Requirements

- **Python 3.11+**
- **macOS 13+** or **Windows 10/11**
- An **API key** from your chosen provider — the app does not ship a key and cannot function without one.
- OS permissions (see [OS permissions](#os-permissions)).

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/todanley/PC.git lulubot
cd lulubot
python3 -m venv .venv
source .venv/bin/activate         # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-app.txt
```

### 2. Set your API key

**Option A — environment variable / `.env` file** (recommended for developers):

Copy `.env.example` → `.env` and fill in the block for your provider, or export directly:

```bash
# Claude (default)
export ANTHROPIC_API_KEY=sk-ant-...

# Gemini (Google AI Studio)
export GEMINI_API_KEY=AIza...
export PHANTOM_PROVIDER=google
export PHANTOM_MODEL=gemini-2.5-flash-lite

# Kimi (Moonshot)
export ANTHROPIC_API_KEY=sk-...            # your Moonshot key
export PHANTOM_PROVIDER=moonshot
export PHANTOM_MODEL=kimi-k2.5
# Chinese-region Moonshot console keys use platform.moonshot.cn:
# export PHANTOM_MOONSHOT_URL=https://api.moonshot.cn/v1/chat/completions
```

**Option B — in-app settings dialog** (recommended for non-technical users):

Launch the app and click **⚙ API Key** in the toolbar. Fill in provider, model, and key. Settings persist to your local config folder — never sent anywhere except directly to your chosen AI provider on each turn.

Config folder:
- macOS: `~/Library/Application Support/LuLuBot/settings.json`
- Windows: `%LOCALAPPDATA%\LuLuBot\settings.json`

### 3. Run

```bash
python3 -m app.main
```

---

## Running a task

1. Launch LuLuBot.
2. In the task input, describe what you want in natural language, e.g.:
   - `Open Chrome, go to news.ycombinator.com, and read the top comment on the top story.`
   - `In Notion, create a new page titled "Weekly review" with today's date.`
   - `打开抖音桌面版，进入我的关注列表，把每个男性账号取消关注。`
3. Click **Run**.
4. Watch the log panel — each turn shows the model's reasoning, the chosen action, and any verification result.
5. Click **Stop** at any time to cancel cleanly (the current turn finishes, then the loop exits).

The app minimizes itself when a task starts so its own window doesn't appear in the screenshots.

### Headless / scripted runs

Useful for testing and CI:

```bash
PHANTOM_TASK="open Safari and go to example.com" \
PHANTOM_AUTORUN=1 \
PHANTOM_LOG_TO_STDERR=1 \
python3 -m app.main
```

---

## The action loop in detail

Each turn the vision model must return a JSON object with a single top-level action. The runner supports:

| Action | Purpose | Fields |
|---|---|---|
| `click` | Left-click at logical coordinates | `x`, `y` |
| `right_click` | Right-click | `x`, `y` |
| `double_click` | Double-click | `x`, `y` |
| `type` | Insert text at the focus | `text` |
| `key` | Send a single key or chord | `key` (e.g. `enter`, `cmd+space`, `ctrl+w`) |
| `scroll` | Wheel-scroll the point under the cursor | `direction`, optional `x`/`y` |
| `drag` | Press → move → release | `from_x`, `from_y`, `to_x`, `to_y` |
| `wait` | Sleep before next screenshot | `seconds` |
| `done` | Task complete | `reason` |
| `stuck` | Give up | `reason` |

Coordinates are always in **logical** pixels (top-left origin), matching what the model sees at screenshot resolution. The runner handles retina scaling internally.

**Verification.** After a click, the runner diffs a 200×200 crop around the click site against the previous screenshot. If both the mean-pixel delta and the count of significantly-changed pixels are below threshold, it records a `noop` — the model sees that hint on the next turn and can re-target instead of assuming the click landed. See `_click_was_noop` in `app/runner.py`.

**No-op detection also runs on scrolls** to detect end-of-list conditions.

**Rate limiting.** Set `PHANTOM_ACTIONS_PER_MIN` and `PHANTOM_TASKS_PER_HOUR` to cap runaway loops. `PHANTOM_MAX_STEPS` caps total turns per task.

**Loop-break heuristic.** `PHANTOM_LOOPBREAK` (default on) detects when the model has repeated the same action N times in a row on visually-identical screenshots and forces `stuck`, so a broken flow can't burn credit forever.

---

## Configuration reference

Every knob is an env var. The most-used ones are also editable in the **⚙ API Key** dialog.

### Provider & model

| Env var | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude or Kimi API key (env name reused for Moonshot). |
| `GEMINI_API_KEY` | — | Google AI Studio key. Presence flips `gemini-*` models from browser to API. |
| `PHANTOM_MODEL` | `claude-opus-4-7` | Any model ID your provider accepts. |
| `PHANTOM_PROVIDER` | auto | `anthropic` / `google` / `moonshot` / `gemini`. |
| `PHANTOM_API_BASE` | provider default | Override the base URL (proxy / self-hosted gateway). |
| `PHANTOM_MOONSHOT_URL` | `api.moonshot.ai/...` | Set to `api.moonshot.cn/...` for Chinese-region Moonshot keys. |
| `PHANTOM_GEMINI_CLI` | `~/.claude/tools/gemini.py` | Path to the Playwright-based Gemini CLI (browser mode only). |

### Vision & prompting

| Env var | Default | Description |
|---|---|---|
| `PHANTOM_JPEG_QUALITY` | `75` | JPEG quality for the screenshot sent to the model. Lower = fewer tokens. |
| `PHANTOM_SOM` | `0` | `1` to overlay OCR text + icon contours (Set-of-Mark). |
| `PHANTOM_SOM_UIA` | `0` | Windows only — merge UI Automation elements into the marks. |
| `PHANTOM_SOM_ICONS`, `PHANTOM_SOM_MAX_MARKS`, `PHANTOM_SOM_OCR_CONF`, … | see `app/som.py` | Fine-tune the SoM pass. |

### Runner behavior

| Env var | Default | Description |
|---|---|---|
| `PHANTOM_MAX_STEPS` | `60` | Hard cap on turns per task. |
| `PHANTOM_ACTIONS_PER_MIN` | unset | Actions-per-minute rate limit. |
| `PHANTOM_TASKS_PER_HOUR` | unset | Tasks-per-hour rate limit. |
| `PHANTOM_POST_ACTION_DELAY_S` | small | Sleep between action dispatch and next screenshot. |
| `PHANTOM_SCROLL_STEP`, `PHANTOM_SCROLL_CLICKS` | tuned | Wheel step size + click count per scroll. |
| `PHANTOM_HUMANIZE` | `1` | `0` disables mouse jitter / typing delays. |
| `PHANTOM_LOOPBREAK` | `1` | `0` disables the repeated-action loop breaker. |
| `PHANTOM_FOCUS_APP` | unset | App name to keep frontmost every turn (macOS `osascript`). |
| `PHANTOM_CTRL_KEY` | platform default | Override the modifier the model's `ctrl+…` chords resolve to (macOS translates to `cmd+…` by default). |
| `PHANTOM_RECORD` | `1` | `0` to skip the per-run `screen.mp4`. |
| `PHANTOM_RUN_DIR` | `phantom_run_<ts>/` | Custom output dir for screenshots, marks, video, turn dumps. |
| `PHANTOM_TURN_DUMP` | `0` | `1` to dump full model input/output JSON per turn. |
| `PHANTOM_SCROLL_UIA` | `0` | Windows only — try to scroll via UIA before falling back to the wheel. |

### Test hooks

| Env var | Default | Description |
|---|---|---|
| `PHANTOM_TASK` | unset | Prefill the task input on launch. |
| `PHANTOM_AUTORUN` | `0` | `1` + `PHANTOM_TASK` → click Run automatically. |
| `PHANTOM_LOG_TO_STDERR` | `0` | Mirror the log panel to stderr for shell monitoring. |

---

## Windows platform improvements

The Windows build has received a round of grounding-, launch-, and runner-reliability fixes on top of the cross-platform base. Each one was driven by a specific failure mode observed in the baseline suite; they are all live in `main`.

### App launch (agent-side)

- **Always use the keyboard launcher.** Every "open X" now resolves to Win+R (`Cmd+Space` on macOS) → type executable name → Enter, instead of the previous mouse-first "click the taskbar/desktop icon" default. Circular Chromium-family icons (Chrome, Edge, iQiyi, Feishu) are visually indistinguishable at screenshot resolution, and gemini-3.5-flash reliably picked the wrong one, landing the agent in a different browser with a different logged-in state for the rest of the run. App names are unambiguous; icons aren't. Skip only when the requested app is already foreground. (`app/vision.py` SYSTEM_PROMPT)
- **Close existing instances before launching a fresh one.** If the target app already has any window open, the agent focuses each and sends Alt+F4 (clicking "Discard" on any save prompt) before the keyboard-launcher sequence. Without this, sessions inherit whatever the previous run left behind — a paused video detail page, a half-typed compose box, an open modal — and spend the first 30+ turns unwinding to the home surface instead of starting the actual task.

### Harness foreground hygiene

- **Unconditional `Shell.MinimizeAll` before agent start.** Previously this fired only on `--chrome-profile` / `--url` runs, so organic (no-URL) runs inherited whatever window (usually the operator's terminal or IDE) was in front. The agent's Win+R keystrokes then competed with that window — "type chrome" landed as text in VS Code, "enter" fired there, and the Run dialog never appeared. Hoisted the MinimizeAll call out of `_foreground_chrome` and made it run on every Windows harness invocation.

### UIA + OCR grounding (Set-of-Mark)

- **Drop over-wide UIA boxes that contain ≥2 OCR sub-boxes.** When a UIA element's bounding box spatially contains two or more OCR text-box centres, the UIA wrapper is dropped before dedup runs. Fixes the Douyin profile header, where a single `<a>` link wraps four visible labels (`关注 N`, live-indicator badge, `粉丝 N`, `获赞`). Previously the UIA wrapper was kept and the OCR sub-boxes were deduped away, so clicking the wrapper mark landed on its centre — usually the live-stream badge, never the follow count — and the follow modal never opened, sending the model into an infinite loop. Tight UIA boxes (0–1 contained OCR centres) are unaffected. (`app/som.py`)

### Runner reliability

- **Fragmented MP4 recording.** Each ~1 s GOP is now a self-contained fragment with its own index, so a hard `TerminateProcess` kill (what the harness `--timeout` triggers on Windows) still leaves a decodable file. Previously the `moov` atom was only written at clean shutdown; a killed run produced a multi-GB `mdat`-only stream that every player refused (`moov atom not found`). Costs a few percent file-size overhead per run; worth it because the graceful path is the exception in long test sessions. (`app/runner.py`)
- **Per-turn debug dumps wired at both inference sites.** Both the Gemini-CLI and OpenAI-compatible inference paths now call `_dump_turn`, so `mark_NN.png` lands next to `turn_NN.md` with the full model request + response for that screenshot. Previously the helper existed but the two inline dump blocks still wrote only the combined `turns.txt`, so the pairing never appeared on disk. (`app/vision.py`)

---

## Baseline regression suite

`tools/baseline_tests.sh {all|1|2|3|4|5}` runs the canonical five-task regression that gates every change touching `app/vision.py`, `app/runner.py`, or `app/som.py`. Each task text is fixed in the script so results stay comparable across sessions.

| # | Task | What it exercises |
|---|---|---|
| 1 | Douyin follow-list blacklist (`我的 → 关注`, unfollow male accounts) | UIA + OCR SoM on a dense Chinese-language profile grid; list traversal; per-row action buttons |
| 2 | Gmail self-send | Cross-app launch via Win+R; web compose UI; text input into rich-text field |
| 3 | Sydney → Guangzhou flight search | Multi-step form fill (date pickers, autocomplete airports); result-page comprehension |
| 4 | Captcha resolution | reCAPTCHA / Cloudflare Turnstile / hCaptcha demo pages — image-grid clicks, checkbox targeting, drag puzzles |
| 5 | Douyin DM to 10 creators with >50k fans | Threshold filter (parse `粉丝 N` from profile cards); recommended-stream traversal; open DM compose pane and send a non-trivial Chinese message |

Console logs from prior runs are kept under `_runs_baseline_*` for post-mortem diffing.

---

## OS permissions

### macOS

System Settings → **Privacy & Security**:

- **Screen Recording** — grant to your terminal (dev) or `LuLuBot.app` (packaged). Without this, screenshots are blank.
- **Accessibility** — grant to the same. Without this, mouse/keyboard events silently no-op.
- **Automation** (as prompted) — for `PHANTOM_FOCUS_APP`'s AppleScript `System Events` calls.

You'll need to fully quit and relaunch after granting.

### Windows

- Run as a regular user. If your target app is elevated (Admin), LuLuBot must be too, or SendInput events are silently dropped by UIPI.
- Windows Defender may flag the PyInstaller bundle. Sign it, or exclude the folder.

---

## Building a standalone binary

Install PyInstaller into the same venv:

```bash
pip install pyinstaller
```

**macOS:**

```bash
pyinstaller --noconfirm --onedir --windowed \
  --name LuLuBot \
  --add-data "app/knowledge.md:app" \
  app/main.py
# Output: dist/LuLuBot/LuLuBot.app
```

**Windows (PowerShell):**

```powershell
pyinstaller --noconfirm --onedir --windowed `
  --name LuLuBot `
  --add-data "app/knowledge.md;app" `
  app/main.py
# Output: dist\LuLuBot\LuLuBot.exe
```

Notes:
- The bundle is ~120–180 MB (PySide6 + RapidOCR ONNX models + bundled ffmpeg).
- On macOS, code-sign + notarize before distributing, or Gatekeeper will block the first launch.
- Users still need to grant Screen Recording + Accessibility permissions to the packaged app.

---

## Project structure

```
app/
  main.py            — QApplication bootstrap, autorun hook, clean-shutdown wiring
  ui.py              — PySide6 main window, task input, log panel, settings dialog
  runner.py          — QThread that runs the vision loop; no-op detection, rate limiting, recording
  vision.py          — VisionClient: screenshot encoding, provider routing, HTTP calls, JSON parsing
  humanize.py        — Mouse-path jitter and per-character type delays
  som.py             — Set-of-Mark overlay (RapidOCR text + OpenCV icon contours + UIA merge)
  uia_win.py         — Windows UI Automation accessibility element source (SoM enrichment)
  settings.py        — Local JSON settings (~/Library/Application Support/LuLuBot/ or %LOCALAPPDATA%\LuLuBot\)
  knowledge.md       — Domain knowledge appended to the system prompt each turn
  platform_layer/
    __init__.py      — capabilities(), platform-appropriate Input & Screen exports
    mac.py           — Quartz CGEvent input, screencapture / mss screenshots, AppleScript focus
    win.py           — pyautogui/pywin32 input, mss screenshots, foreground-window focus

tools/
  baseline_tests.sh  — 5-task regression suite (see [Baseline regression suite](#baseline-regression-suite))
  run_and_review.py  — helper to launch a run and diff its output against a reference

requirements-app.txt — Python deps (Python 3.11+)
.env.example         — placeholder env var template — copy to .env
.gitignore
README.md            — this file
```

Runtime output per task lands in `phantom_run_<timestamp>/`:
- `screen.mp4` — full session recording (unless `PHANTOM_RECORD=0`)
- `turn_NN_before.jpg`, `turn_NN_after.jpg` — screenshots
- `mark_NN.png` — SoM overlay (if enabled)
- `turn_NN.md` — per-turn model request + response (if `PHANTOM_TURN_DUMP=1`)

---

## Knowledge base (`app/knowledge.md`)

Every turn's system prompt includes the contents of `app/knowledge.md`. It has two kinds of entries:

- **General patterns** — cross-app rules (launch via keyboard launcher, navigate back with mouse not `escape`, follow buttons encode current state not action, verify toggles landed, …).
- **App-specific flows** — under `## App: <name>` headings. Concrete step-by-step recipes for one product (e.g. how to open Douyin's Following roster, how the video player's action rail is laid out).

When you discover a non-obvious quirk while debugging a run, **add it to `knowledge.md`** — the next run starts already knowing it, and future model versions inherit the tribal knowledge.

---

## Troubleshooting

**Screenshots come out blank / all-black (macOS).**
Screen Recording permission isn't granted, or was granted to the old Python binary from a previous venv. Re-grant to the current one (System Settings → Privacy → Screen Recording), then fully quit and relaunch.

**Mouse events go nowhere (macOS).**
Accessibility permission missing on the same binary. Same fix.

**Model keeps returning `stuck` on the first turn.**
Usually a permission issue — the screenshot is blank so the model has nothing to work with. Check the saved `turn_00_before.jpg` under `phantom_run_*/`.

**Agent presses Win+R but nothing opens (Windows).**
Foreground-window contention — the launcher keystrokes landed in the operator's terminal/IDE instead of the Run dialog. Fixed for harness-driven runs (unconditional `MinimizeAll` before start); for direct GUI runs, minimize/hide other windows before clicking Run.

**Recorded MP4 won't open / `moov atom not found`.**
You're on an old checkout — pull latest. The runner now writes fragmented MP4 so even a hard-killed run produces a decodable file.

**`certifi` / SSL errors on macOS.**
`pip install --upgrade certifi` inside the venv. `VisionClient` deliberately uses `certifi`'s CA bundle rather than the system store.

**Gemini browser mode opens Chromium but never returns.**
Make sure you're logged into gemini.google.com in the Chrome profile Playwright loads. First run may need a captcha solve.

**Moonshot key returns 401.**
If your key is from `platform.moonshot.cn` (Chinese console), set `PHANTOM_MOONSHOT_URL=https://api.moonshot.cn/v1/chat/completions`. The default endpoint is `.ai`, which rejects `.cn` keys.

**"Runner destroyed while still running" on Cmd+Q.**
Fixed in `app/main.py::_install_clean_shutdown` — if you're on an old checkout, pull latest.

**PyInstaller bundle crashes with `ImportError: rapidocr_onnxruntime`.**
Add `--collect-all rapidocr_onnxruntime` to your `pyinstaller` command.

---

## FAQ

**Does it upload anything besides screenshots?**
Screenshots + your task text + turn history go to your chosen provider on each turn. Nothing goes anywhere else. No LuLuBot backend exists.

**Can it interact with password fields / 2FA?**
Yes — it can type into any focused text field. Whether that's a good idea is up to you. The recorded `screen.mp4` will contain anything on screen; if you don't want that, set `PHANTOM_RECORD=0`.

**Why not use the DOM / accessibility tree?**
We do, optionally (`PHANTOM_SOM=1`, `PHANTOM_SOM_UIA=1` on Windows). But making it the primary source ties the agent to browsers/native controls and breaks on games, canvas apps, remote desktops, and half of Electron. The vision-first design generalizes.

**Can it play games?**
Simple UI-driven ones, yes. Anything requiring reaction time under ~2 s, no — the round-trip to a vision LLM is the floor.

**Costs?**
Depends on provider, model, screenshot resolution, and task length. A 20-turn Claude Opus task at retina resolution is ~$0.20–$0.50. The same task on Gemini 2.5 Flash Lite is ~$0.01–$0.03. Kimi K2.5 sits in between. Set `PHANTOM_JPEG_QUALITY=60` and `PHANTOM_MAX_STEPS` to cap costs.

**Does it work with the Anthropic `computer_20250124` tool?**
No — LuLuBot uses regular `/v1/messages` with vision, not the beta computer-use tool. Coordinates are chosen by the model from raw pixels.

---

## Safety notes

LuLuBot has full mouse and keyboard control of your machine while running. Some baseline hygiene:

- **Don't leave it running unattended on important sessions.** A confused model will happily click "Delete Account" if the task ambiguously asks for cleanup.
- **Prefer a scratch user account** for exploratory automation.
- **Cap runaway loops** with `PHANTOM_MAX_STEPS`, `PHANTOM_ACTIONS_PER_MIN`, `PHANTOM_TASKS_PER_HOUR`.
- **Review `.env` before committing.** `.gitignore` excludes it, but audit anyway.
- **Respect target sites' ToS.** Automating logins, follows, likes, or scraping may violate the platform you're using. That's on you.
- **The screen recording captures everything on screen** — including notifications, messages, other apps. Disable via `PHANTOM_RECORD=0` if that's a concern.

---

## Contributing

PRs welcome. Some good starter areas:

- Add app-specific recipes to `app/knowledge.md` for products you use.
- New provider adapters in `app/vision.py` (OpenAI, Bedrock, Vertex, Ollama, …).
- Better no-op detection for scrolls in complex layouts.
- macOS/Windows accessibility parity in `app/platform_layer/`.

Keep changes surgical, don't add abstractions for hypothetical future providers, and please run the app end-to-end on both macOS and Windows if you touch `platform_layer/`. If you touch anything in the SoM / vision / runner path, run `tools/baseline_tests.sh all` before opening the PR.

---

## License

MIT
