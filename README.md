# LuLuBot 噜噜机器人

A **semi-computer-use** desktop automation agent powered by AI vision.

> **Semi-computer-use** means LuLuBot operates purely through vision: it takes a screenshot each turn, sends it to an AI model, receives a structured action (click, type, scroll, drag, …), and executes it via native OS input APIs — no DOM access, no browser extensions, no accessibility-tree injection. Each decision is grounded in the actual pixels on screen, not a parsed DOM tree. This makes it work across any application — native apps, games, web apps in any browser.

---

## How it works

```
┌──────────────────────────────────────────────────────┐
│                   Your screen                        │
└──────────────────┬───────────────────────────────────┘
                   │ screenshot
                   ▼
          ┌─────────────────┐
          │  AI vision model │  ← Claude / Gemini / Kimi
          │  (your API key)  │
          └────────┬─────────┘
                   │ JSON action
                   ▼
         click / type / scroll / drag
                   │
                   ▼
            next screenshot → repeat
```

The agent runs a multi-turn loop. It sees the screen, decides the next single action, executes it, then looks at the result — until the task is done or it declares itself stuck.

---

## Supported AI providers

| Provider | Model examples | Key env var |
|---|---|---|
| **Anthropic (Claude)** | `claude-opus-4-7` (default) | `ANTHROPIC_API_KEY` |
| **Google AI Studio (Gemini)** | `gemini-2.5-flash`, `gemini-2.5-flash-lite` | `GEMINI_API_KEY` |
| **Moonshot (Kimi)** | `kimi-k2.5`, `kimi-k2.6` | `ANTHROPIC_API_KEY` (reused) |
| **Gemini via browser** | any `gemini-*` | none (uses your logged-in Chrome session) |

---

## Requirements

- Python 3.11+
- macOS 13+ or Windows 10/11
- An API key from your chosen provider (see table above)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/todanley/PC.git lulubot
cd lulubot
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-app.txt
```

### 2. Set your API key

**Option A — environment variable (recommended for developers):**

```bash
# Claude
export ANTHROPIC_API_KEY=sk-ant-...

# Gemini
export GEMINI_API_KEY=AIza...
export PHANTOM_MODEL=gemini-2.5-flash
export PHANTOM_PROVIDER=google

# Kimi
export ANTHROPIC_API_KEY=sk-...   # your Moonshot key
export PHANTOM_MODEL=kimi-k2.5
export PHANTOM_PROVIDER=moonshot
```

**Option B — in-app settings:**

Launch the app and click **⚙ API Key** in the toolbar. Fill in your provider, model, and key — saved to your local config folder, never sent anywhere except directly to your chosen AI provider.

### 3. Run

```bash
python3 -m app.main
```

---

## Configuration reference

| Env var | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | API key for Claude or Kimi |
| `GEMINI_API_KEY` | — | API key for Google AI Studio (Gemini) |
| `PHANTOM_MODEL` | `claude-opus-4-7` | Model name to use |
| `PHANTOM_PROVIDER` | auto-detected | `anthropic` / `google` / `moonshot` / `gemini` |
| `PHANTOM_API_BASE` | — | Override API base URL (e.g. for a proxy) |
| `PHANTOM_JPEG_QUALITY` | `75` | JPEG compression for screenshots sent to the model |
| `PHANTOM_SOM` | `0` | Set to `1` to enable Set-of-Mark visual grounding (RapidOCR + contour detection overlaid on screenshots) |

---

## Building a standalone binary

Install PyInstaller, then run:

```bash
# macOS
pip install pyinstaller
pyinstaller --noconfirm --onedir --windowed \
  --name LuLuBot \
  --add-data "app/knowledge.md:app" \
  app/main.py

# Windows (PowerShell)
pip install pyinstaller
pyinstaller --noconfirm --onedir --windowed `
  --name LuLuBot `
  --add-data "app/knowledge.md;app" `
  app/main.py
```

The binary appears in `dist/LuLuBot/`.

---

## Project structure

```
app/
  main.py          — entry point (QApplication bootstrap)
  ui.py            — PySide6 main window
  runner.py        — QThread that drives the vision loop
  vision.py        — VisionClient: screenshot → API call → action JSON
  som.py           — Set-of-Mark visual grounding (opt-in)
  uia_win.py       — Windows UI Automation accessibility source (opt-in)
  platform_layer/  — macOS / Windows input & screenshot abstraction
  settings.py      — local settings storage (API key, provider, model)
  knowledge.md     — domain knowledge injected into the system prompt
requirements-app.txt
```

---

## License

MIT
