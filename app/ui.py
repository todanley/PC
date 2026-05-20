"""PySide6 UI for Phantom-Click."""
import json
import os
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QVBoxLayout, QWidget,
)

from . import wallet
from .build_config import IS_CN_BUILD
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
QLabel { color: #e6e8eb; font-size: 27px; }
QPlainTextEdit {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 12px;
    padding: 18px; color: #e6e8eb; font-size: 30px;
}
QPlainTextEdit:focus { border: 1px solid #5a8dff; }
QPushButton {
    background: #5a8dff; color: white; border: none; border-radius: 12px;
    padding: 17px 38px; font-weight: 600; font-size: 30px;
}
QPushButton:hover { background: #6a9aff; }
QPushButton:pressed { background: #4a7def; }
QPushButton:disabled { background: #2a2f38; color: #6b7180; }
QPushButton#stopBtn { background: #2a2f38; color: #e6e8eb; }
QPushButton#stopBtn:hover { background: #3a3f48; }
QPushButton#stopBtn:disabled { background: #1a1d23; color: #4b5160; }
QPushButton#linkBtn {
    background: transparent; color: #8ba3ff; padding: 4px 0;
    font-weight: 500; font-size: 16px; text-align: left;
}
QPushButton#linkBtn:hover { color: #a8baff; }
QPushButton#replayBtn {
    background: #2a2f38; color: #c9cdd4; border: 1px solid #3a3f48;
    border-radius: 6px; padding: 6px 12px; font-weight: 500; font-size: 14px;
    font-family: "SF Mono", Menlo, Consolas, monospace;
}
QPushButton#replayBtn:hover { background: #353b46; color: #e6e8eb; }
QPushButton#replayBtn:pressed { background: #1f242c; }
QListWidget {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 8px;
    padding: 6px; color: #c9cdd4; font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 16px;
}
QFrame#turnCard {
    background: #1a1d23; border: 1px solid #2a2f38; border-radius: 8px;
}
QLabel#turnHeader { color: #8b9099; font-size: 14px; font-weight: 600; }
QLabel#sectionLabel { color: #8b9099; font-size: 13px; font-weight: 600; }
QLabel#monoText {
    color: #c9cdd4; font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 14px; background: #0f1115; padding: 6px; border-radius: 4px;
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

        # Screenshot thumbnail + debug-info row.
        shot = payload.get("screenshot")
        if shot:
            pm = QPixmap(shot)
            if not pm.isNull():
                thumb = QLabel()
                thumb.setPixmap(pm.scaledToWidth(360, Qt.SmoothTransformation))
                v.addWidget(thumb)
            # One-line metadata + an "Open at full size" button so the user
            # can inspect exactly the image the model received (the JPEG sent
            # over the wire is a re-encode of this PNG; for click coord
            # debugging the dimensions and visible content are identical).
            try:
                w = pm.width(); h = pm.height()
            except Exception:
                w = h = 0
            import os
            info_row = QHBoxLayout()
            info_row.setContentsMargins(0, 0, 0, 0)
            meta = QLabel(f"📷 {w}×{h}px · {os.path.basename(shot)}")
            meta.setObjectName("monoText")
            meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
            meta.setWordWrap(True)
            info_row.addWidget(meta, stretch=1)
            open_btn = QPushButton("Open")
            open_btn.setObjectName("linkBtn")
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.clicked.connect(
                lambda _=False, p=shot: __import__("subprocess").Popen(
                    ["open", p]
                )
            )
            info_row.addWidget(open_btn)
            v.addLayout(info_row)

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
        self.setWindowTitle("噜噜机器人")
        self.setMinimumSize(1400, 820)
        self.setStyleSheet(DARK_QSS)
        self._runner: TaskRunner | None = None
        self._system_prompt: str = ""

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Title
        title = QLabel("噜噜机器人")
        title.setFont(QFont("", 49, QFont.Bold))
        layout.addWidget(title)

        sub = QLabel("告诉我要做什么，我来替你操作电脑。")
        sub.setStyleSheet("color: #8b9099; font-size: 29px;")
        layout.addWidget(sub)

        layout.addSpacing(6)

        # Wallet balance line (CN-ship builds only). Shows the user their own
        # remaining quota in dollars — never Gemini tokens. Updated from the
        # bridge's X-Quota-Remaining-Usd header after each turn.
        self._last_remaining: float | None = None
        if IS_CN_BUILD:
            self.balance_label = QLabel(self._balance_text())
            self.balance_label.setStyleSheet("color: #8b9099; font-size: 20px;")
            self.balance_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(self.balance_label)
            layout.addSpacing(6)

        # Task input
        prompt_label = QLabel("想让我做什么？")
        layout.addWidget(prompt_label)

        self.task_input = QPlainTextEdit()
        self.task_input.setPlaceholderText(
            '例如："打开计算器，计算 17 × 24"'
        )
        self.task_input.setFixedHeight(152)
        layout.addWidget(self.task_input)

        # Controls row
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)

        self.run_btn = QPushButton("▶  运行")
        self.run_btn.clicked.connect(self._on_run)
        ctrl_row.addWidget(self.run_btn)

        self.stop_btn = QPushButton("■  停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        ctrl_row.addWidget(self.stop_btn)

        ctrl_row.addStretch(1)

        self.debug_btn = QPushButton("调试")
        self.debug_btn.setObjectName("linkBtn")
        self.debug_btn.setCheckable(True)
        self.debug_btn.toggled.connect(self._on_toggle_debug)
        ctrl_row.addWidget(self.debug_btn)

        self.sys_btn = QPushButton("查看系统提示")
        self.sys_btn.setObjectName("linkBtn")
        self.sys_btn.clicked.connect(self._on_show_system)
        ctrl_row.addWidget(self.sys_btn)

        layout.addLayout(ctrl_row)
        layout.addSpacing(8)

        # 进度 (left, brief activity log) + 对话 (right, full per-turn
        # debug card with screenshot, prompt, response, replay button).
        # Splitter so user can drag the divider.
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        log_label = QLabel("进度")
        log_label.setStyleSheet("color: #8b9099; font-size: 22px;")
        lv.addWidget(log_label)
        self.log = QListWidget()
        lv.addWidget(self.log, stretch=1)
        splitter.addWidget(left)

        self._debug_panel = QWidget()
        rv = QVBoxLayout(self._debug_panel)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)
        conv_label = QLabel("对话与调试")
        conv_label.setStyleSheet("color: #8b9099; font-size: 22px;")
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
        splitter.addWidget(self._debug_panel)
        # Hidden by default — the "调试" toggle in the controls row shows it.
        self._debug_panel.hide()

        self._splitter = splitter
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

    # ── Wallet token / balance ──────────────────────────────────────────
    def _balance_text(self) -> str:
        if not wallet.get_token():
            return "令牌：未设置（运行时输入）"
        if self._last_remaining is None:
            return "令牌：已设置"
        return f"剩余额度：${self._last_remaining:.2f}"

    def _refresh_balance_label(self):
        if IS_CN_BUILD and hasattr(self, "balance_label"):
            self.balance_label.setText(self._balance_text())

    def _prompt_token(self, reason: str = "") -> bool:
        """Modal prompt for the user's wallet token. Returns True if a token
        was entered and stored."""
        prefix = (reason + "\n\n") if reason else ""
        tok, ok = QInputDialog.getText(
            self, "输入令牌", prefix + "请输入管理员给你的令牌（token）：")
        if ok and tok.strip():
            wallet.set_token(tok.strip())
            self._last_remaining = None
            self._refresh_balance_label()
            return True
        return False

    def _on_quota_updated(self, remaining: float):
        self._last_remaining = remaining
        self._refresh_balance_label()

    def _on_run(self):
        task = self.task_input.toPlainText().strip()
        if not task:
            self._append_log("[!] 请先输入任务。")
            return
        if self._runner and self._runner.isRunning():
            return

        # CN-ship builds need a wallet token before they can call the bridge.
        if IS_CN_BUILD and not wallet.get_token():
            if not self._prompt_token("尚未设置令牌。"):
                self._append_log("[!] 未输入令牌，无法运行。")
                return

        self.log.clear()
        self._clear_conversation()
        self._append_log(f"▶  开始：{task}")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.task_input.setReadOnly(True)

        self._runner = TaskRunner(task)
        self._runner.step_started.connect(self._on_step_started)
        self._runner.step_done.connect(self._on_step_done)
        self._runner.turn_logged.connect(self._on_turn_logged)
        self._runner.finished_ok.connect(self._on_finished_ok)
        self._runner.failed.connect(self._on_failed)
        self._runner.quota_updated.connect(self._on_quota_updated)
        self._runner.start()

    def _on_stop(self):
        if self._runner:
            self._runner.cancel()
            self._append_log("[已请求停止 — 正在结束当前步骤…]")
            self.stop_btn.setEnabled(False)

    def closeEvent(self, event):
        """Window close handler. If a task is running, an in-flight vision
        call sits in a blocking urllib socket read for up to several seconds
        (sometimes 30s+). Qt's QThread::terminate() doesn't interrupt that
        because pthread_cancel can't break a C-level socket recv. If we let
        Qt run its destruction sequence anyway, QCoreApplication tears down
        with the TaskRunner thread still alive — Qt logs that as fatal and
        aborts the process, producing the shutdown SIGABRT we kept seeing
        in crash reports.

        Strategy: give the runner a brief window to honor cancel cleanly
        (good case, no Worker request in flight). If it doesn't return,
        bypass Qt's destructors entirely with os._exit() — there's no
        unsaved state at window-close time, and the OS reclaims sockets.
        """
        if self._runner and self._runner.isRunning():
            self._runner.cancel()
            if not self._runner.wait(3_000):
                # Vision call mid-flight. Skip Qt cleanup and exit hard.
                event.accept()
                os._exit(0)
        event.accept()

    def _on_show_system(self):
        dlg = _SystemPromptDialog(self._system_prompt, self)
        dlg.exec()

    def _on_step_started(self, step: int, msg: str):
        # Suppress all "I'm working" chatter — recorder paths, screenshot
        # captures, vision calls. Users only see WHAT the agent does, not
        # the AI plumbing. Action lines come through _on_step_done.
        return

    def _on_step_done(self, step: int, action: dict):
        act = action.get("action", "?")
        reason = action.get("reasoning", "")
        label = {
            "click": "点击", "double_click": "双击", "type": "输入",
            "key": "按键", "scroll": "滚动", "drag": "拖动",
            "wait": "等待", "done": "完成",
            "rejected_menubar_click": "已拒绝（菜单栏区域）",
            "suppressed_repeat_click": "已忽略（重复点击）",
        }.get(act, act)
        if act == "click":
            detail = f"({action.get('x')},{action.get('y')})"
        elif act == "type":
            detail = repr(action.get("text", ""))
        elif act == "key":
            detail = repr(action.get("key", ""))
        elif act == "scroll":
            detail = action.get("direction", "")
        elif act == "drag":
            detail = f"({action.get('x1')},{action.get('y1')})→({action.get('x2')},{action.get('y2')})"
        else:
            detail = ""
        line = f"  → {label} {detail}".rstrip()
        if reason:
            line += f"  — {reason}"
        self._append_log(line)
        prog = action.get("progress")
        if prog:
            self._append_log(f"     [进度] {prog}")

    def _on_toggle_debug(self, checked: bool):
        """Show / hide the right-hand debug pane (per-turn cards with
        screenshot + USER + ASSISTANT sections). The activity log on the
        left stays visible either way."""
        if checked:
            self._debug_panel.show()
            # Restore a sensible split; if the user dragged it earlier,
            # this resets it to a usable proportion.
            self._splitter.setSizes([320, 640])
        else:
            self._debug_panel.hide()

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
        self._append_log(f"✓  完成：{msg}")
        self._reset_buttons()

    def _on_failed(self, msg: str):
        # Wallet/quota messages: show verbatim and prompt for a (new) token so
        # the user can top up and rerun without restarting the app.
        if msg.startswith(("额度", "请输入", "令牌")):
            if msg.startswith("额度"):
                self._last_remaining = 0.0
                self._refresh_balance_label()
            self._append_log(f"✗  {msg}")
            self._reset_buttons()
            self._prompt_token(msg)
            return
        # Strip internal run-dir / video paths from failure messages so the
        # demo UI stays clean. Keep the human-readable head before the colon.
        clean = msg
        for marker in ("Run dir:", "Video:", "/var/folders/", "/tmp/"):
            i = clean.find(marker)
            if i >= 0:
                clean = clean[:i].rstrip(" ·•")
                break
        if clean.startswith("cancelled"):
            clean = "已取消"
        elif clean.startswith("stuck"):
            clean = "卡住了，已自动停止"
        self._append_log(f"✗  {clean}")
        self._reset_buttons()

    def _reset_buttons(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.task_input.setReadOnly(False)
