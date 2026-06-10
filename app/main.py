"""LuLuBot desktop app entry point."""
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import wallet
from .build_config import IS_CN_BUILD
from .ui import MainWindow


def _prompt_token_on_launch_if_missing(win: MainWindow) -> None:
    """If this is a CN-ship build and the user has no token stored locally,
    show the token-input modal immediately after the window appears. This
    is the first-launch flow for distributed builds: every new install
    starts with no quota and no token (operator manually mints tokens via
    worker/issue-token.sh and hands them out), so the very first thing
    the user must do is paste theirs in.

    We schedule the prompt after a short delay so the main window has a
    chance to render first — the modal is then visibly anchored to the
    app, not floating in the dark with nothing behind it. Skipped during
    PHANTOM_AUTORUN (test/CI runs that bring their own token via env)."""
    if not IS_CN_BUILD:
        return
    if wallet.get_token():
        return
    if os.environ.get("PHANTOM_AUTORUN") == "1":
        return

    def _ask():
        win._prompt_token(
            "欢迎使用噜噜机器人。\n首次启动还没有令牌，请向管理员索取后输入：")

    QTimer.singleShot(600, _ask)


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
    app.setApplicationName("LuLuBot")
    win = MainWindow()
    _install_clean_shutdown(app, win)
    _wire_autorun(win)
    _prompt_token_on_launch_if_missing(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
