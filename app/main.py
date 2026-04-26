"""Phantom-Click desktop app entry point."""
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .ui import MainWindow


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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Phantom-Click")
    win = MainWindow()
    _wire_autorun(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
