# UnboundComputerUse — Resume Summary

Quotable one-page summary of the technical work in this repository. Written to be dropped into a job-application project entry as-is; trim and reorder as needed.

## Project one-liner

**UnboundComputerUse** — Vision-first, cross-platform (macOS + Windows) computer-use agent that drives arbitrary desktop and web applications via LLM-inferred mouse/keyboard actions on raw screenshots. **~9,800 LOC** across a Python/PySide6 client, a Cloudflare Worker + D1/R2 backend, and a marketing site.

## Architecture

- Designed a **vision-first agent loop** that decides one action per turn from a single JPEG screenshot — no DOM, no accessibility tree required — generalizing across native apps, browsers, games, and remote desktops. Verified each action via a 200×200 px pixel-diff to detect no-op clicks.
- Built a **provider-abstract vision layer** routing between Anthropic Claude, Google Gemini (REST + OpenAI-compat), Moonshot Kimi (OpenAI-compat), and a Playwright-driven `gemini.google.com` fallback — swappable via env var; ~25× cost delta between best/cheapest providers.
- Implemented a **cross-platform input/screen adapter** (`platform_layer/mac.py` + `win.py`) exposing a unified `Input` / `Screen` API atop Quartz CGEvent on macOS and pyautogui/pywin32/mss on Windows.
- Packaged as **PyInstaller single-file `.exe` on Windows and code-signed `.app` on macOS** via a bespoke build pipeline that injects bridge secrets at build time via `@@PLACEHOLDER@@` sed substitution.

## Grounding — Set-of-Mark, OCR, and UI Automation

- Implemented **Set-of-Mark (SoM) prompting** (Yang et al. 2023) as a lightweight hybrid: **RapidOCR** (PP-OCR Chinese + English via **ONNX Runtime**) for text-bearing controls, **OpenCV** edge-contour proposals for icon-only controls, and Windows **UI Automation (UIA)** for semantic controls that vision can't distinguish (`···` overflow buttons, small icon glyphs, desktop launch icons).
- Wrote a **492-LOC Windows UIA traversal** with control-type filtering, time-budgeted BFS (1 s budget, depth 25), and a Chrome page-`Document` fast path. Detects Chromium renderers via `--force-renderer-accessibility=complete` so **the entire DOM shows up as UIA nodes** with pixel-accurate bounding boxes.
- Designed a novel **over-wide UIA drop rule**: when a UIA element's bounding box spatially contains ≥2 OCR text-box centres, the wrapper element is discarded before dedup — fixes the common "link wraps four labels" failure mode (Douyin profile header: `<a>` containing `关注 N`, live badge, `粉丝 N`, `获赞`).
- Reduced the vision model's task from **coordinate regression to element-number selection**, cutting misclick rates on dense Chinese-language UIs.

## Captcha resolution

- Solved **slider-puzzle, click-the-shapes, drag-into-shadow, rotate-to-upright, image-grid, and "I'm not a robot" captchas** without any hardcoded solver — the LLM identifies the challenge type from the on-screen instruction text and emits generic `drag`/`click` primitives that match the puzzle geometry.
- A/B tested Gemini 3-Pro vs Gemini 3.5-Flash on slider puzzles at 200% entry-zoom: Flash converged in ~5 attempts, Pro oscillated past 18; adopted Flash as CN-ship default at ~4× cost reduction.
- Added an entry-zoom auto-heuristic (reset then bump to 200%) so tiny CAPTCHA glyphs remained readable after downscaling for the vision encoder.

## Behavioral humanization (anti-detection)

- Built a **9-primitive input humanization stack** (`humanize.py`) to defeat browser-side behavioral fingerprinting: cubic-Bezier mouse paths with perpendicular-curve bias + sub-pixel micro-tremor; **Fitts's-law duration model** (~600 px/s peak with ±20% jitter); **log-normal per-key delays with English-bigram weighting**; probabilistic typos with QWERTY-adjacent correction; hover pauses; idle cursor micro-wiggle; ease-in/out timing; sub-task break simulation.
- Implemented **sliding-window action-rate cap** (25 actions/min default) and a **persistent tasks-per-hour counter** (JSON on disk under `%LOCALAPPDATA%` / `~/Library/Application Support`) to prevent runaway loops from tripping platform anti-abuse.
- Added a **loop-break heuristic**: avg-hash Hamming-distance across a 10-frame sliding window; when the current screenshot matches ≥5 of the last 10 within Hamming ≤6, force `stuck` — cuts wasted API spend on broken flows.

## Reliability engineering

- **Fragmented-MP4 screen recording** — each ~1 s GOP is a self-contained fragment with its own index, so hard `TerminateProcess` kills still leave a decodable file (the default `moov`-at-end layout produced unplayable multi-GB `mdat`-only streams on `--timeout` kill).
- **In-run done-list** — model reports `done_item` on each list-processing step; the runner echoes the full history back under "✅ ALREADY PROCESSED" every turn, so long list traversals survive scroll-resets, captcha interrupts, and page reloads.
- **Environment-hygiene primitives** — unconditional `Shell.MinimizeAll` before agent start (fixes `Win+R` keystrokes routing to the wrong foreground window), close-existing-instances-before-launch, always-keyboard-launcher (Win+R / Cmd+Space) to avoid ambiguous circular-icon misclicks on Chrome / Edge / iQiyi.
- Per-turn debug pairing: `mark_NN.png` + `turn_NN.md` on disk so any single frame can be inspected alongside the exact model request/response.

## Backend (Cloudflare Worker + D1 + R2)

- Wrote a **TypeScript Cloudflare Worker** exposing the OpenAI-compatible Gemini surface, adding bearer-token auth + prepaid wallet metering: on each request, validate bearer → refuse 402 if balance spent → forward → meter real Gemini `usage` (including reasoning tokens billed at output rate) → apply operator markup (basis-point) → atomic `UPDATE ... RETURNING` on D1 → echo remaining balance in `X-Quota-Remaining-Usd` header.
- Built a **CN-accessible download proxy**: since `github.com` / `objects.githubusercontent.com` are intermittently blocked from mainland China, the Worker performs a two-step GitHub Releases API dance (auth'd 302 → signed S3 URL without auth) and streams the private-repo asset back through Cloudflare's edge.
- Set up a **GitHub Actions matrix build** (`macos-14` + `windows-latest`) → PyInstaller → R2 upload (versioned + `-latest` alias) → `version.json` manifest → Astro marketing site deploy to Cloudflare Pages, all triggered by tag push.
- Ran a **retired FastAPI+cloudflared bridge** during Mac-dev phase (per-IP sliding-window rate limit, atomic tokens-file mutation via `asyncio.Lock`, log-per-request) before migrating to the Worker.

## Client UI (PySide6, ~950 LOC)

- Dark-themed desktop UI with **per-turn debug pane** rendering screenshot thumbnail + user prompt + model response as one card per turn, with a "Replay move to (x, y)" button that smoothly moves the real cursor to the model's chosen coordinates for pixel-accuracy debugging.
- **Frameless always-on-top status bubble** shown while the agent runs so a demo viewer sees live step-by-step status without occluding the workspace; click-to-restore the main window; drag-to-reposition.
- Wallet balance line with **masked token preview** (`pc_xxxx…yyyy`) — never reveals the full token to screen-recording capture.
- Clean-shutdown wiring on `aboutToQuit` + `closeEvent` — hard-exits via `os._exit` if a vision call is mid-flight in a blocking `urllib` socket read, avoiding a SIGABRT that would otherwise fire when the `QThread` destructor sees a live thread.

## Regression testing

- Wrote a **5-task baseline suite** (`tools/baseline_tests.sh`) exercising every production path: Chinese social-graph traversal (Douyin follow-list filter-and-unfollow), auth'd webmail (Gmail self-send), complex external-site navigation (Sydney → Guangzhou flight search), captcha resolution, batched Chinese-language DM sends to filtered creators (>50k fans). Each task runs via a headless `run_and_review.py` harness that captures screenshots, marked frames, per-turn model dumps, and full-run MP4.

## Tech stack

**Languages:** Python 3.11, TypeScript, PowerShell, Bash, Astro/HTML/CSS
**AI / CV:** Claude, Gemini, Kimi/Moonshot, RapidOCR, ONNX Runtime, OpenCV
**Frameworks:** PySide6/Qt, FastAPI, Playwright, PyInstaller
**Infrastructure:** Cloudflare Workers, D1 (SQLite), R2, Pages, cloudflared tunnels, GitHub Actions
**Platform APIs:** Windows UI Automation (via comtypes), pywin32/Win32 (SendInput, AttachThreadInput, ShowWindow, `Shell.MinimizeAll`), Quartz CGEvent, mss, ffmpeg, `--force-renderer-accessibility`

## Impact-shaped bullets (if your resume prefers outcome framing)

- Built and shipped a cross-platform vision-first computer-use agent (**~10 k LOC**) that reliably drives arbitrary desktop and web software — including native Chinese apps and the four common captcha families — using only screenshots and a vision LLM.
- Implemented a **detection-free grounding pipeline** combining OCR, edge-contour proposals, and Windows UI Automation, reducing misclick rate on dense multilingual UIs and unlocking Chromium DOM as first-class accessibility structure.
- Cut per-run API cost **~4×** by A/B-testing vision models on the captcha path and switching the CN-ship default from Gemini Pro to Flash without regressing task success.
- Designed a **Cloudflare-based backend** (Worker + D1 wallet + R2 + Pages + edge-proxied private-repo download) that ships to mainland-China users past GitHub's intermittent reachability, with atomic prepaid metering and per-model pricing markups.
