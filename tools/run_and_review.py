"""Self-review harness: run an agent task, capture every turn, surface the
artifacts for inspection, and iterate.

It drives the SAME app the user runs (`python -m app.main`) via the autorun
hooks in app/main.py, but pins all per-turn artifacts to a known directory so
they can be reviewed afterwards:

    _runs/<timestamp>_<label>/
        step_NN.png     raw screenshot the runner captured that turn
        mark_NN.png     the Set-of-Mark-annotated image the model actually saw
        turns.txt       system prompt + per-turn user text + model response
        stderr.log      mirrored activity log (✓ done / ✗ failure lines)
        meta.json       task, args, exit reason, artifact counts

Completion: the app's Qt window stays open after a task ends, so we can't just
wait on process exit. Instead we tail stderr (PHANTOM_LOG_TO_STDERR=1) for the
runner's terminal marker (a line starting with ✓ or ✗) and also enforce a hard
timeout, then terminate the app.

Usage (from the repo root, with bridge creds in the environment):

    python tools/run_and_review.py \
        --task "Open douyin.com ..." \
        --label readonly-followlist \
        --chrome-profile "Profile 3" --url https://www.douyin.com \
        --max-steps 12 --timeout 300

The agent SEIZES the real mouse/keyboard/screen while it runs — don't touch
the machine until it reports done.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _find_chrome() -> str | None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _launch_chrome(profile_dir: str, url: str) -> subprocess.Popen | None:
    chrome = _find_chrome()
    if not chrome:
        print("  [chrome] chrome.exe not found — skipping pre-launch")
        return None
    args = [chrome]
    if profile_dir:
        args.append(f"--profile-directory={profile_dir}")
    # Build Chrome's full renderer accessibility tree eagerly so the SoM UIA
    # source (PHANTOM_SOM_UIA) sees page elements from turn 1, instead of a
    # thin browser-chrome-only tree that fills in a turn or two later.
    # Harness-only — a reason this isn't baked into the shipped app yet.
    args.append("--force-renderer-accessibility")
    args.append("--new-window")
    if url:
        args.append(url)
    print(f"  [chrome] launching: {profile_dir!r} -> {url}")
    try:
        return subprocess.Popen(args)
    except Exception as e:
        print(f"  [chrome] launch failed: {e}")
        return None


def _foreground_chrome() -> None:
    """Windows: clear the desktop and bring the stanley-profile Chrome window
    to the foreground before the agent takes its first screenshot.

    `_launch_chrome` uses `--new-window` so Chrome SHOULD foreground itself,
    but a separate terminal / IDE window (e.g. Claude Code's TUI, which sits
    in its own process and so is NOT covered by `_minimize_own_console`) can
    steal focus back during the chrome-warmup OR mid-run when the user clicks
    it. We minimise EVERYTHING first (Shell.Application.MinimizeAll), then
    restore Chrome — so no other window is visible to steal focus when the
    agent clicks near the Chrome edge. AttachThreadInput bypasses Windows'
    cross-process SetForegroundWindow restriction.
    """
    if sys.platform != "win32":
        return
    try:
        import win32gui, win32con, ctypes
    except Exception:
        return
    # Step 1: minimise everything (including IDE / terminals / overlays).
    try:
        import win32com.client
        win32com.client.Dispatch("Shell.Application").MinimizeAll()
        time.sleep(0.4)
    except Exception:
        pass
    cands = []
    def _cb(h, _):
        try:
            if win32gui.GetClassName(h) == "Chrome_WidgetWin_1":
                t = win32gui.GetWindowText(h)
                if t and "Google Chrome" in t:
                    cands.append((h, t))
        except Exception:
            pass
        return True
    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return
    if not cands:
        return
    # Prefer a douyin-titled window when multiple Chrome windows exist.
    cands.sort(key=lambda c: 0 if ("抖音" in c[1] or "douyin" in c[1].lower()) else 1)
    hwnd = cands[0][0]
    try:
        # MAXIMIZE (not just RESTORE) so Chrome fills the screen and can't be
        # accidentally covered if a small overlay re-appears mid-run.
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    except Exception:
        pass
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        cur = kernel32.GetCurrentThreadId()
        fg = user32.GetForegroundWindow()
        fg_pid = ctypes.c_ulong()
        fg_thread = user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        attached = False
        if fg_thread and fg_thread != cur:
            attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_thread, False)
    except Exception:
        pass


def _minimize_own_console() -> None:
    """Windows: drop THIS harness's console window out of the way before the
    agent starts driving the screen.

    The agent reads the foreground window's pixels every turn. A visible
    console (the terminal we launched from) sits on top of the browser, so the
    agent wastes turns trying to close it to see the page — and can even close
    the very terminal hosting this run, killing it mid-task. Minimizing our own
    console at startup makes the timing exact (it happens before app.main even
    launches) and is harness-only: the shipped GUI exe has no console at all.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    except Exception:
        pass


def main() -> int:
    # The agent's logs and tasks are Chinese; the Windows console defaults to
    # cp1252 and crashes on them. Force UTF-8 (replace anything unmappable).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="task string for the agent")
    ap.add_argument("--label", default="run", help="short label for the run dir")
    ap.add_argument("--max-steps", type=int, default=0,
                    help="cap turns (0 = uncapped)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="hard wall-clock limit in seconds")
    ap.add_argument("--chrome-profile", default="",
                    help="Chrome profile-directory to pre-launch (e.g. 'Profile 3')")
    ap.add_argument("--url", default="", help="URL to pre-open in Chrome")
    ap.add_argument("--chrome-warmup", type=float, default=4.0,
                    help="seconds to wait after launching Chrome before the agent starts")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ROOT / "_runs" / f"{stamp}_{args.label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    turns = run_dir / "turns.txt"
    stderr_log = run_dir / "stderr.log"
    print(f"== run dir: {run_dir}")

    chrome_proc = None
    if args.chrome_profile or args.url:
        chrome_proc = _launch_chrome(args.chrome_profile, args.url)
        if chrome_proc:
            time.sleep(args.chrome_warmup)

    env = dict(os.environ)
    env.update({
        "PHANTOM_RUN_DIR": str(run_dir),
        "PHANTOM_TASK": args.task,
        "PHANTOM_AUTORUN": "1",
        "PHANTOM_LOG_TO_STDERR": "1",
        "PHANTOM_TURN_DUMP": str(turns),
        "PHANTOM_MAX_STEPS": str(args.max_steps),
        # SoM UI Automation element source (app/uia_win.py) — on by default in
        # the harness; set PHANTOM_SOM_UIA=0 in the environment for an A/B
        # baseline run without it.
        "PHANTOM_SOM_UIA": os.environ.get("PHANTOM_SOM_UIA", "1"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    missing = [k for k in ("PHANTOM_BRIDGE_URL", "PHANTOM_BRIDGE_TOKEN")
               if not env.get(k)]
    if missing:
        print(f"!! warning: {missing} not set — vision calls will fail unless "
              "another provider (GEMINI_API_KEY / ANTHROPIC_API_KEY) is configured")

    print(f"== launching: python -m app.main  (task: {args.task!r})")
    # Get our console out of the way so it can't obscure (or be closed by) the
    # browser the agent is about to drive. Do it last, right before launch.
    _minimize_own_console()
    # When the user explicitly opted into pre-launching Chrome via the harness
    # (--chrome-profile / --url), also raise that Chrome window so a separate
    # terminal/IDE window can't dominate the agent's first screenshot. When
    # neither flag is passed (agent-driven test), do NOT touch window state —
    # the whole point is to test the agent without harness pre-arrangement.
    if args.chrome_profile or args.url:
        _foreground_chrome()
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True, encoding="utf-8", errors="replace",
        # Don't let the child spawn its own visible console window either
        # (defense in depth; app.main is a Qt GUI and needs no console).
        creationflags=(subprocess.CREATE_NO_WINDOW
                       if sys.platform == "win32" else 0),
    )

    done = {"reason": None}
    lines: list[str] = []

    def _pump():
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            print("   | " + line, flush=True)
            stripped = line.strip()
            # Runner terminal markers mirrored by the UI log.
            if stripped.startswith("✓"):
                done["reason"] = done["reason"] or "completed"
            elif stripped.startswith("✗"):
                done["reason"] = done["reason"] or ("failed: " + stripped)

    t = threading.Thread(target=_pump, daemon=True)
    t.start()

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            done["reason"] = done["reason"] or "process exited"
            break
        if done["reason"]:
            # Give the app a moment to flush the final frame, then stop.
            time.sleep(2.0)
            break
        time.sleep(0.5)
    else:
        done["reason"] = f"timeout after {args.timeout}s"

    # Tear down the app (its window lingers after a task ends).
    for p in (proc,):
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    try:
        stderr_log.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    steps = sorted(p.name for p in run_dir.glob("step_*.png"))
    marks = sorted(p.name for p in run_dir.glob("mark_*.png"))
    meta = {
        "task": args.task,
        "label": args.label,
        "max_steps": args.max_steps,
        "timeout": args.timeout,
        "chrome_profile": args.chrome_profile,
        "url": args.url,
        "exit_reason": done["reason"],
        "step_frames": len(steps),
        "mark_frames": len(marks),
        "turns_dump": turns.exists(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                       encoding="utf-8")

    print("\n== RESULT")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print(f"\n== artifacts in: {run_dir}")
    for n in steps:
        print("   step  ", n)
    for n in marks:
        print("   mark  ", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
