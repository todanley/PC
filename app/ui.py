"""PySide6 UI for Phantom-Click."""
import json
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
    QVBoxLayout, QWidget,
)

from .platform_layer import Input
from .runner import TaskRunner


def _replay_move_to(x: float, y: float):
    """Smooth-move the cursor to (x, y) on a background thread so the Qt
    main thread stays responsive while the bezier animation plays.
    No click — debug-only verification of AI-returned coordinates."""
    def go():
        try:
            Input().move_to(float(x), float(y))
        except Exception:
            # Silent — this is debug aid; failures shouldn't crash the UI.
            pass
    threading.Thread(target=go, daemon=True).start()


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
QPushButton#linkBtn {
    background: transparent; color: #8ba3ff; padding: 4px 0;
    font-weight: 500; font-size: 12px; text-align: left;
}
QPushButton#linkBtn:hover { color: #a8baff; }
QPushButton#replayBtn {
    background: #2a2f38; color: #c9cdd4; border: 1px solid #3a3f48;
    border-radius: 6px; padding: 6px 12px; font-weight: 500; font-size: 11px;
    font-family: "SF Mono", Menlo, Consolas, monospace;
}
QPushButton#replayBtn:hover { background: #353b46; color: #e6e8eb; }
QPushButton#replayBtn:pressed { background: #1f242c; }
QListWidget {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 8px;
    padding: 6px; color: #c9cdd4; font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
}
QFrame#turnCard {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 8px;
}
QLabel#turnHeader { color: #8b9099; font-size: 11px; font-weight: 600; }
QLabel#sectionLabel { color: #8b9099; font-size: 10px; font-weight: 600; }
QLabel#monoText {
    color: #c9cdd4; font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 11px; background: #0f1115; padding: 6px; border-radius: 4px;
}
QScrollArea { background: #0f1115; border: none; }
"""


class _SystemPromptDialog(QDialog):
    def __init__(self, system_prompt: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System prompt")
        self.resize(720, 600)
        layout = QVBoxLayout(self)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(system_prompt or "(not yet sent — start a task first)")
        layout.addWidget(edit)


class _TurnCard(QFrame):
    """One turn: header + screenshot thumbnail + user text + response."""

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("turnCard")
        self.setFrameShape(QFrame.NoFrame)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        step = payload.get("step", "?")
        action = payload.get("action") or {}
        act = action.get("action", "?")
        header = QLabel(f"Turn {step}  ·  {act}")
        header.setObjectName("turnHeader")
        v.addWidget(header)

        # Screenshot thumbnail
        shot = payload.get("screenshot")
        if shot:
            pm = QPixmap(shot)
            if not pm.isNull():
                thumb = QLabel()
                thumb.setPixmap(pm.scaledToWidth(360, Qt.SmoothTransformation))
                v.addWidget(thumb)

        # User prompt
        ulabel = QLabel("USER")
        ulabel.setObjectName("sectionLabel")
        v.addWidget(ulabel)
        utext = QLabel(payload.get("user_text", ""))
        utext.setObjectName("monoText")
        utext.setWordWrap(True)
        utext.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(utext)

        # Response
        rlabel = QLabel("ASSISTANT")
        rlabel.setObjectName("sectionLabel")
        v.addWidget(rlabel)
        # Prefer pretty-printed JSON if the action parses; else show raw text.
        resp = payload.get("response_text", "")
        parsed = None
        try:
            parsed = json.loads(resp)
            resp_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            resp_pretty = resp
        rtext = QLabel(resp_pretty)
        rtext.setObjectName("monoText")
        rtext.setWordWrap(True)
        rtext.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(rtext)

        # Debug aid: when the model emitted a click, show a button that
        # smooth-moves the real cursor to (x, y) so the user can visually
        # verify whether the AI-returned coords actually land on the
        # intended element. Move only — no click — to avoid mutating UI
        # state while debugging.
        if isinstance(parsed, dict) and parsed.get("action") in ("click", "double_click"):
            x = parsed.get("x")
            y = parsed.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                btn = QPushButton(f"▶  Replay move to ({x}, {y})")
                btn.setObjectName("replayBtn")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda _=False, X=x, Y=y: _replay_move_to(X, Y))
                row.addWidget(btn)
                row.addStretch(1)
                v.addLayout(row)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Phantom-Click")
        self.setMinimumSize(900, 720)
        self.setStyleSheet(DARK_QSS)
        self._runner: TaskRunner | None = None
        self._system_prompt: str = ""

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

        self.sys_btn = QPushButton("View system prompt")
        self.sys_btn.setObjectName("linkBtn")
        self.sys_btn.clicked.connect(self._on_show_system)
        ctrl_row.addWidget(self.sys_btn)

        layout.addLayout(ctrl_row)

        layout.addSpacing(6)

        # Activity log + Conversation, side-by-side via splitter so the
        # user can resize. Activity is brief; Conversation shows the full
        # prompt/screenshot/response per turn.
        splitter = QSplitter(Qt.Horizontal)

        # Left: activity log (brief)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        log_label = QLabel("Activity")
        log_label.setStyleSheet("color: #8b9099; font-size: 12px;")
        lv.addWidget(log_label)
        self.log = QListWidget()
        lv.addWidget(self.log, stretch=1)
        splitter.addWidget(left)

        # Right: conversation panel (rich)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)
        conv_label = QLabel("Conversation")
        conv_label.setStyleSheet("color: #8b9099; font-size: 12px;")
        rv.addWidget(conv_label)
        self.conv_scroll = QScrollArea()
        self.conv_scroll.setWidgetResizable(True)
        self._conv_inner = QWidget()
        self._conv_layout = QVBoxLayout(self._conv_inner)
        self._conv_layout.setContentsMargins(4, 4, 4, 4)
        self._conv_layout.setSpacing(10)
        self._conv_layout.addStretch(1)  # bottom spacer; cards inserted before it
        self.conv_scroll.setWidget(self._conv_inner)
        rv.addWidget(self.conv_scroll, stretch=1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([320, 640])
        layout.addWidget(splitter, stretch=1)

    def _append_log(self, text: str, color: str = "#c9cdd4"):
        item = QListWidgetItem(text)
        item.setForeground(Qt.GlobalColor.lightGray)
        self.log.addItem(item)
        self.log.scrollToBottom()

    def _clear_conversation(self):
        # Remove every widget except the trailing stretch spacer.
        while self._conv_layout.count() > 1:
            it = self._conv_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    def _on_run(self):
        task = self.task_input.toPlainText().strip()
        if not task:
            self._append_log("[!] Type a task first.")
            return
        if self._runner and self._runner.isRunning():
            return

        self.log.clear()
        self._clear_conversation()
        self._append_log(f"▶  Starting: {task}")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.task_input.setReadOnly(True)

        self._runner = TaskRunner(task)
        self._runner.step_started.connect(self._on_step_started)
        self._runner.step_done.connect(self._on_step_done)
        self._runner.turn_logged.connect(self._on_turn_logged)
        self._runner.finished_ok.connect(self._on_finished_ok)
        self._runner.failed.connect(self._on_failed)
        self._runner.start()

    def _on_stop(self):
        if self._runner:
            self._runner.cancel()
            self._append_log("[stop requested — finishing current step…]")
            self.stop_btn.setEnabled(False)

    def _on_show_system(self):
        dlg = _SystemPromptDialog(self._system_prompt, self)
        dlg.exec()

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

    def _on_turn_logged(self, payload: dict):
        sysp = payload.get("system_prompt")
        if sysp:
            self._system_prompt = sysp
        card = _TurnCard(payload)
        # Insert above the trailing stretch spacer.
        self._conv_layout.insertWidget(self._conv_layout.count() - 1, card)
        # Auto-scroll so the new card is visible. setValue(maximum) is
        # racy — Qt hasn't finished computing the new max when we ask for
        # it, even with QTimer(0). Use ensureWidgetVisible() which waits
        # until the inner widget is laid out, plus a delayed
        # belt-and-suspenders setValue() to cover the case where the new
        # card's height grows after the first visibility request.
        def _scroll_to_card():
            self.conv_scroll.ensureWidgetVisible(card, 0, 0)
            bar = self.conv_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        QTimer.singleShot(0, _scroll_to_card)
        QTimer.singleShot(80, _scroll_to_card)

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
