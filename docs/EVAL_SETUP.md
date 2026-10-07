# Running the baseline tests on a fresh machine (Mac or Windows)

Short setup to reproduce the same comparison runs that live in `_runs/`. Written so a clone-and-go takes under 10 minutes once you have the keys.

## 1. Clone and venv

```bash
git clone https://github.com/todanley/PC.git unbound-computer-use
cd unbound-computer-use

python3 -m venv .venv

# macOS / Linux:
source .venv/bin/activate
# Windows (PowerShell):
#   .venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements-app.txt
```

The eval test scripts (`tools/baseline_tests.sh`, `tools/run_task5_gemini.sh`, `tools/compare_douyin.sh`) autodetect the venv python at `.venv/bin/python` (macOS/Linux) or `.venv/Scripts/python.exe` (Windows), so no cross-platform tweaks are needed.

## 2. API keys — create `_env.sh` (gitignored)

Create `_env.sh` at the repo root with your own keys. **Do not commit it** — the `/_*` rule in `.gitignore` keeps it out of git automatically.

```bash
# ROTATE these if you ever paste them in chat / commit by mistake.
export ANTHROPIC_API_KEY='sk-ant-usr-...'
export GEMINI_API_KEY='AIza...'                 # Google AI Studio key

# Legacy — not needed if you go direct via provider SDKs:
# export PHANTOM_BRIDGE_URL='https://bridge.z1nexusn1.org'
# export PHANTOM_BRIDGE_TOKEN='pc_...'
```

Mint fresh keys at:
- Anthropic: https://console.anthropic.com/settings/keys
- Google AI Studio: https://aistudio.google.com/apikey

## 3. Chrome profile — log in to Douyin once, manually

The Douyin baseline tests need a Chrome profile that's **already logged in** to `douyin.com`. The agent won't log in for you — it drives the UI post-login.

- Open Chrome manually.
- Pick (or create) a dedicated profile (click the avatar top-right → Add).
- Log into douyin.com in that profile.
- Note the profile directory name — it's shown in Chrome's `chrome://version` page under "Profile Path" (e.g. `.../User Data/Profile 3` → use `"Profile 3"`).

Pass that string to the test scripts via `--chrome-profile "Profile 3"` (the three current scripts have this hard-coded to `Profile 3`; edit if your profile has a different name).

## 4. Run

### Just the Douyin follow + blacklist baseline (TASK 1):

```bash
source _env.sh
tools/baseline_tests.sh 1
```

### Just the Douyin comment baseline (TASK 5):

```bash
source _env.sh
tools/baseline_tests.sh 5
```

### Direct model comparison on TASK 5 with Gemini 3.8 Flash (no bridge):

```bash
source _env.sh
tools/run_task5_gemini.sh
```

### Full suite (all four + the task-5 comment addition):

```bash
source _env.sh
tools/baseline_tests.sh all
```

### A/B comparison harness (sonnet5 vs gemini38, serial):

```bash
source _env.sh
tools/compare_douyin_all.sh
```

## 5. Artifacts

Each run drops a labeled directory under `_runs/<timestamp>_<label>/`:

- `screen.mp4` — full session recording
- `step_NN.png` — raw screenshot per turn
- `mark_NN.png` — SoM-annotated frames (what the model actually saw)
- `turn_NN.md` — per-turn system prompt + user text + model response (easy to grep)
- `turns.txt` — same content as above but combined
- `meta.json` — task, label, exit reason, step count
- `stderr.log` — mirrored harness output

Also one console log per run at the repo root: `_runs_<label>_console.log`.

Everything under `_runs/` and `_runs_*.log` is gitignored, so artifacts stay local.

## 6. Which model is wired where

- **Dev / local default** (any `python -m app.main` or `tools/run_and_review.py` with no overrides): `claude-sonnet-5` (set in `app/vision.py`).
  - ⚠️ **Known plumbing bug** — our Anthropic adapter does not currently handle Sonnet 5's `thinking` content block; needs a 3-line fix in `_anthropic_response_text`. Until that's in, prefer the Gemini path for actual test runs.
- **Gemini direct** (what `tools/run_task5_gemini.sh` and `tools/compare_douyin.sh` do): set `PHANTOM_PROVIDER=google` + `PHANTOM_MODEL=gemini-3.8-flash` + a `GEMINI_API_KEY`. Bypasses the (currently drained) CF bridge wallet.
- **Bridge path** (CN-ship default via `PHANTOM_BRIDGE_URL` + `PHANTOM_BRIDGE_TOKEN`): quota exhausted on the shared token as of 2026-10-07; mint a fresh token or use direct provider keys as above.

Current model/provider is dumped into `meta.json`'s `task` field is captured in the agent's first turn's log — grep the first `turn_01.md` for `MODEL` or inspect the console log.

## 7. What to watch for

- **Don't touch the mouse or keyboard** while a run is in flight — the agent seizes OS-level input. Even a stray mouse jiggle derails a turn.
- The agent self-reports `done_item` entries into `turn_NN.md`. These are *claims*, not verified facts — spot-check `step_NN.png` or `screen.mp4` for ground truth. (See `docs/eval/compare_2026-10-07_sonnet5_vs_gemini38.md` for a worked example of this pitfall.)
- On macOS, grant **Screen Recording** and **Accessibility** to your terminal (or the Python binary) in System Settings → Privacy & Security before the first run — otherwise screenshots are blank and clicks silently no-op.
- `_env.sh` is gitignored. If you want secrets managed differently (direnv, 1Password CLI, shell profile), the test scripts just want the env vars present — swap `source _env.sh` for whatever your flow is.
