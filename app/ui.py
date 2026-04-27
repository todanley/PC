"""PySide6 UI for Phantom-Click."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .runner import TaskRunner


DARK_QSS = """
QMainWindow, QWidget { background: #0f1115; color: #e6e8eb; }
QLabel { color: #e6e8eb; }
QPlainTextEdit {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 8px;
    padding: 10px; color: #e6e8eb; font-size: 14px;
}
QPlainTextEdit:focus { border: 1px solid #5a8dff; }
QPushButton {
    background: #5a8dff; color: white; border: none; border-radius: 8px;
    padding: 10px 22px; font-weight: 600; font-size: 14px;
}
QPushButton:hover { background: #6a9aff; }
QPushButton:pressed { background: #4a7def; }
QPushButton:disabled { background: #2a2f38; color: #6b7180; }
QPushButton#stopBtn { background: #2a2f38; color: #e6e8eb; }
QPushButton#stopBtn:hover { background: #3a3f48; }
QPushButton#stopBtn:disabled { background: #1a1d23; color: #4b5160; }
QListWidget {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 8px;
    padding: 6px; color: #c9cdd4; font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Phantom-Click")
        self.setMinimumSize(640, 520)
        self.setStyleSheet(DARK_QSS)
        self._runner: TaskRunner | None = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Title
        title = QLabel("Phantom-Click")
        title.setFont(QFont("", 22, QFont.Bold))
        layout.addWidget(title)

        sub = QLabel("Tell me what to do, I'll take over your screen.")
        sub.setStyleSheet("color: #8b9099; font-size: 13px;")
        layout.addWidget(sub)

        layout.addSpacing(6)

        # Task input
        prompt_label = QLabel("What should I do?")
        layout.addWidget(prompt_label)

        self.task_input = QPlainTextEdit()
        self.task_input.setPlaceholderText(
            'e.g., "Open Calculator and compute 17 × 24"'
        )
        self.task_input.setFixedHeight(90)
        layout.addWidget(self.task_input)

        # Controls row
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)

        self.run_btn = QPushButton("▶  Run")
        self.run_btn.clicked.connect(self._on_run)
        ctrl_row.addWidget(self.run_btn)

        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        ctrl_row.addWidget(self.stop_btn)

        ctrl_row.addStretch(1)

        layout.addLayout(ctrl_row)

        layout.addSpacing(6)

        # Activity log
        log_label = QLabel("Activity")
        log_label.setStyleSheet("color: #8b9099; font-size: 12px;")
        layout.addWidget(log_label)

        self.log = QListWidget()
        layout.addWidget(self.log, stretch=1)

    def _append_log(self, text: str, color: str = "#c9cdd4"):
        item = QListWidgetItem(text)
        item.setForeground(Qt.GlobalColor.lightGray)
        self.log.addItem(item)
        self.log.scrollToBottom()

    def _on_run(self):
        task = self.task_input.toPlainText().strip()
        if not task:
            self._append_log("[!] Type a task first.")
            return
        if self._runner and self._runner.isRunning():
            return

        self.log.clear()
        self._append_log(f"▶  Starting: {task}")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.task_input.setReadOnly(True)

        self._runner = TaskRunner(task)
        self._runner.step_started.connect(self._on_step_started)
        self._runner.step_done.connect(self._on_step_done)
        self._runner.finished_ok.connect(self._on_finished_ok)
        self._runner.failed.connect(self._on_failed)
        self._runner.start()

    def _on_stop(self):
        if self._runner:
            self._runner.cancel()
            self._append_log("[stop requested — finishing current step…]")
            self.stop_btn.setEnabled(False)

    def _on_step_started(self, step: int, msg: str):
        self._append_log(msg)

    def _on_step_done(self, step: int, action: dict):
        act = action.get("action", "?")
        reason = action.get("reasoning", "")
        if act == "click":
            self._append_log(f"  → click ({action.get('x')},{action.get('y')})  — {reason}")
        elif act == "type":
            self._append_log(f"  → type {action.get('text','')!r}  — {reason}")
        elif act == "key":
            self._append_log(f"  → key {action.get('key','')!r}  — {reason}")
        elif act == "scroll":
            self._append_log(f"  → scroll {action.get('direction','')}  — {reason}")
        else:
            self._append_log(f"  → {act}  — {reason}")
        prog = action.get("progress")
        if prog:
            self._append_log(f"     [progress] {prog}")

    def _on_finished_ok(self, msg: str):
        self._append_log(f"✓  Done: {msg}")
        self._reset_buttons()

    def _on_failed(self, msg: str):
        self._append_log(f"✗  {msg}")
        self._reset_buttons()

    def _reset_buttons(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.task_input.setReadOnly(False)
