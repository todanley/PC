"""Phantom-Click desktop app entry point."""
import os
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import wallet
from .build_config import BRIDGE_URL, IS_CN_BUILD
from .ui import MainWindow


def _maybe_fetch_trial_token(win: MainWindow) -> None:
    """First-launch trial-token bootstrap (CN-ship builds only).

    If the app has no token stored locally AND the bridge URL is baked in,
    spin a background thread to fetch a $0.10 trial token from the bridge's
    /mint-trial endpoint. On success, store it and refresh the balance
    label. Silent on failure — the user just sees the normal 'token not
    set' state and can paste an operator-issued token.

    Runs in a thread so the UI never blocks on a slow network. Bridge is
    rate-limited per IP, so spamming app launches doesn't drain the pool."""
    if not IS_CN_BUILD or not BRIDGE_URL:
        return
    if wallet.get_token():
        return  # already have one — leave it alone

    def _worker():
        tok, err = wallet.fetch_trial_token(BRIDGE_URL)
        if tok:
            wallet.set_token(tok)
            # Refresh the balance label on the Qt main thread.
            QTimer.singleShot(0, win._refresh_balance_label)
            QTimer.singleShot(
                0,
                lambda: win._append_log(
                    "🎁  已为您领取 $0.10 试用额度，可立即开始测试。",
                ),
            )
        # Errors deliberately silent — failure just means the user falls
        # through to the normal "enter your token" path. Log to stderr
        # though so an operator running the dev build can see what happened.
        elif err:
            print(f"[trial-token fetch] {err}", file=sys.stderr, flush=True)

    threading.Thread(target=_worker, daemon=True, name="TrialTokenFetch").start()


def _wire_autorun(win: MainWindow) -> None:
    """Test/CI hook: if PHANTOM_TASK is set, prefill it; if PHANTOM_AUTORUN=1,
    click Run shortly after the window appears. Log lines are mirrored to stderr
    so a parent shell can monitor the headless run.
    """
    task = os.environ.get("PHANTOM_TASK")
    if task:
        win.task_input.setPlainText(task)

    if os.environ.get("PHANTOM_LOG_TO_STDERR") == "1":
        def _mirror(text: str, color: str = "#c9cdd4"):
            print(text, file=sys.stderr, flush=True)
            MainWindow._append_log(win, text, color)
        win._append_log = _mirror

    if os.environ.get("PHANTOM_AUTORUN") == "1" and task:
        # Minimize so the agent's screenshots don't include the Phantom-Click
        # window itself — otherwise the model sees its own log ("Step N:
        # capturing screen…") and waits for it.
        def _go():
            win.showMinimized()
            QTimer.singleShot(400, win._on_run)
        QTimer.singleShot(800, _go)


def _install_clean_shutdown(app: QApplication, win: MainWindow) -> None:
    """Wire two cleanup paths so the app never aborts during shutdown:

      • Window close (red X): MainWindow.closeEvent fires — handled there.
      • Cmd+Q / dock-quit / Quit menu: Qt routes this through
        QApplication.quit() which does NOT fire per-window closeEvent. We
        listen on aboutToQuit so the TaskRunner thread is cancelled before
        QCoreApplication tears down. Without this, the QThread destructor
        sees a still-running thread and Qt logs that as fatal → SIGABRT.

    If the runner is in a blocking urlopen and can't honor cancel quickly,
    we hard-exit with os._exit so the destructor never runs.
    """
    def _on_about_to_quit():
        runner = getattr(win, "_runner", None)
        if runner is None or not runner.isRunning():
            return
        runner.cancel()
        # Brief window for in-between-turn cancels (cheap, common case).
        if not runner.wait(3_000):
            os._exit(0)

    app.aboutToQuit.connect(_on_about_to_quit)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Phantom-Click")
    win = MainWindow()
    _install_clean_shutdown(app, win)
    _wire_autorun(win)
    _maybe_fetch_trial_token(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
