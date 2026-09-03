# main_window.py
# 主窗口：立绘展示（异步加载）、聊天气泡、打字机效果、停止生成、
# 拖拽图片、历史恢复、线程调度、迷你模式、定时提醒、空闲主动撩人

import logging
import os
import random
import time
from collections import deque

from PySide6.QtCore import QMutex, Qt, QThread, QTimer, QWaitCondition, Signal
from PySide6.QtGui import QFont, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from browser_pool import BrowserPool
from chat_worker import ChatWorker
from config import (
    MOOD_EMOJI_MAP, PORTRAIT_DISPLAY_WIDTH, PORTRAIT_FOLDER,
    PORTRAIT_SCALE, PROACTIVE_CHECK_INTERVAL_MS, PROACTIVE_IDLE_SECONDS,
    PROACTIVE_LINES, PROACTIVE_MAX_WHILE_AWAY, SYSTEM_PROMPT,
)
from history_store import load_history, save_history
from mini_window import MiniIconWindow
from reminder import ReminderManager
from utils import get_portrait_path, plain_to_html, process_image_command
from widgets import DraggablePortrait, TitleBar

logger = logging.getLogger("jingjing.main_window")

# ---------- 样式常量 ----------
SCROLL_AREA_STYLE = """
    QScrollArea { background: transparent; border: none; }
    QScrollBar:vertical { background: rgba(0,0,0,0.1); width: 6px; margin: 0px; }
    QScrollBar::handle:vertical { background: rgba(0,0,0,0.3); border-radius: 3px; min-height: 10px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
"""

INPUT_STYLE = """
    QTextEdit {
        background-color: rgba(255,255,255,0.85);
        border: 1px solid #CCCCCC;
        border-radius: 4px;
        padding: 3px;
        font-size: 9pt;
    }
"""

IMAGE_BTN_STYLE = """
    QPushButton {
        background-color: #E0E0E0;
        border: none;
        border-radius: 4px;
        font-size: 14px;
    }
    QPushButton:hover { background-color: #C0C0C0; }
"""

STOP_BTN_STYLE = """
    QPushButton {
        background-color: #FF6B6B;
        color: white;
        border: none;
        border-radius: 4px;
        font-size: 12px;
    }
    QPushButton:hover { background-color: #E55353; }
    QPushButton:disabled { background-color: #D0D0D0; }
"""

SEND_BTN_STYLE = """
    QPushButton {
        background-color: #007AFF;
        color: white;
        border: none;
        border-radius: 4px;
        font-size: 10px;
        font-weight: bold;
    }
    QPushButton:hover { background-color: #0055CC; }
    QPushButton:pressed { background-color: #003D99; }
"""

TYPING_LABEL_STYLE = """
    color: #666666;
    background: transparent;
    font-size: 9px;
    padding: 2px 5px;
"""

BUBBLE_STYLE = """
    QFrame {
        background-color: #D4E7FF;
        border-radius: 8px;
        padding: 4px 6px;
        max-width: 200px;
    }
"""

PORTRAIT_MAX_HEIGHT = 450      # 立绘最大显示高度
TYPE_INTERVAL_MS = 25          # 打字机速度：每字间隔（毫秒）
INACTIVE_GAP_SECONDS = 900     # 距上次回复超过该秒数时附加时间提醒
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def _compute_portrait_size(pixmap):
    """计算立绘等比缩放后的显示尺寸（受显示宽度与最大高度约束）"""
    original_width = pixmap.width()
    original_height = pixmap.height()

    scaled_width = int(original_width * PORTRAIT_SCALE)
    scaled_height = int(original_height * PORTRAIT_SCALE)

    if scaled_width > PORTRAIT_DISPLAY_WIDTH:
        ratio = PORTRAIT_DISPLAY_WIDTH / scaled_width
        display_width = PORTRAIT_DISPLAY_WIDTH
        display_height = int(scaled_height * ratio)
    else:
        display_width = scaled_width
        display_height = scaled_height

    if display_height > PORTRAIT_MAX_HEIGHT:
        ratio = PORTRAIT_MAX_HEIGHT / display_height
        display_height = PORTRAIT_MAX_HEIGHT
        display_width = int(display_width * ratio)

    return display_width, display_height


def scale_portrait(pixmap) -> QPixmap:
    """按配置等比缩放立绘（在工作线程中执行）"""
    width, height = _compute_portrait_size(pixmap)
    return pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class PortraitLoader(QThread):
    """后台加载并缩放立绘，避免大图切换时主线程卡顿。"""

    loaded = Signal(int, QPixmap)
    failed = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = deque()
        self._mutex = QMutex()
        self._cond = QWaitCondition()
        self._seq = 0
        self._stop = False

    def submit(self, path: str) -> int:
        """提交一个加载请求，返回请求编号（用于丢弃过期结果）"""
        self._mutex.lock()
        self._seq += 1
        req_id = self._seq
        self._queue.append((req_id, path))
        self._cond.wakeOne()
        self._mutex.unlock()
        return req_id

    def stop(self):
        self._mutex.lock()
        self._stop = True
        self._cond.wakeAll()
        self._mutex.unlock()

    def run(self):
        while True:
            self._mutex.lock()
            while not self._queue and not self._stop:
                self._cond.wait(self._mutex)
            if self._stop and not self._queue:
                self._mutex.unlock()
                break
            req_id, path = self._queue.popleft()
            self._mutex.unlock()
            if self._stop:
                continue  # 停止后弹出的请求直接丢弃：避免加载大图拖慢线程退出
            try:
                pixmap = QPixmap(path)
                if pixmap.isNull():
                    self.failed.emit(req_id, "图片损坏")
                else:
                    self.loaded.emit(req_id, scale_portrait(pixmap))
            except Exception as e:
                self.failed.emit(req_id, str(e))


class MainWindow(QMainWindow):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(580, 500)

        self.client = client
        self.history = self._restore_history()
        # 浏览器池由主窗口统一创建并注入 Worker（依赖注入，便于测试替换）
        self.browser_pool = BrowserPool()
        self.last_reply_time = time.time()

        self.thread = None
        self.worker = None
        # 已停止但线程尚未结束的 (thread, worker) 引用暂存区：
        # 防止 Python GC 在 QThread 退出前析构对象导致闪退
        self._retired = []
        self.current_bubble = None
        self.typing_label = None

        # ---- 打字机相关 ----
        self.char_queue = deque()
        self.typing_timer = QTimer()
        self.typing_timer.timeout.connect(self.type_next_char)
        self.typing_timer.setInterval(TYPE_INTERVAL_MS)

        self.pending_finish = False
        self._pending_remaining = 0  # 最近一次 finished 携带的剩余命令数（批量收尾判定）

        # ---- 立绘异步加载 ----
        self._portrait_seq = 0
        self._portrait_loader = PortraitLoader(self)
        self._portrait_loader.loaded.connect(self._on_portrait_loaded)
        self._portrait_loader.failed.connect(self._on_portrait_failed)
        self._portrait_loader.start()

        # ---- 定时提醒（主窗口持有，注入 Worker） ----
        self.reminder_manager = ReminderManager(self)
        self.reminder_manager.reminder_fired.connect(self._on_reminder_fired)

        # ---- 空闲主动撩人 ----
        self._proactive_count = 0  # 本次"主人未回复"期间的已撩次数（上限 PROACTIVE_MAX_WHILE_AWAY）
        self._proactive_timer = QTimer(self)
        self._proactive_timer.timeout.connect(self._maybe_proactive)
        self._proactive_timer.setInterval(PROACTIVE_CHECK_INTERVAL_MS)
        self._proactive_timer.start()

        self.init_ui()
        self.update_portrait("默认")
        self.add_message("鲸鲸", "欢迎回来，主人～ 今天想聊什么呢？", align_left=True)
        self._setup_mini_mode()

    # ===== 定时提醒 / 空闲主动撩人 =====
    def _on_reminder_fired(self, content: str):
        """提醒到点：回到聊天模式并置前，气泡提示 + 提示音。"""
        if self.mini_window.isVisible():
            self.show_chat_mode()
        self.add_message("鲸鲸", f"⏰ 主人，{content}", align_left=True)
        self.update_portrait("期待")
        QApplication.beep()  # 后台时也能听到提示
        self.raise_()
        self.activateWindow()

    def _maybe_proactive(self):
        """
        空闲超时且无生成中且不在迷你模式 → 鲸鲸主动说一句。
        防刷屏：1) 触发后重置计时（不连续触发）；
        2) 主人回复前最多撩 PROACTIVE_MAX_WHILE_AWAY 次，之后安静等待；
        3) 主人发消息（on_send_clicked）时重置计数。
        """
        if self.worker is not None or self.thread is not None:
            return
        if self.mini_window.isVisible():
            return
        if self._proactive_count >= PROACTIVE_MAX_WHILE_AWAY:
            return  # 本次已撩够，安静等主人
        if time.time() - self.last_reply_time < PROACTIVE_IDLE_SECONDS:
            return
        line = random.choice(PROACTIVE_LINES)
        logger.info("空闲主动撩人（第 %d/%d 次）：%s",
                    self._proactive_count + 1, PROACTIVE_MAX_WHILE_AWAY, line)
        self.add_message("鲸鲸", line, align_left=True)
        self.update_portrait("卖萌")
        self._proactive_count += 1
        self.last_reply_time = time.time()  # 重置，避免连续触发

    # ===== 迷你模式（鼠标中键/双击立绘切换） =====
    def _setup_mini_mode(self):
        """创建立绘触发信号与迷你窗口：中键/双击/右键菜单双向切换。"""
        self.mini_window = MiniIconWindow()
        self.mini_window.back_requested.connect(self.show_chat_mode)
        self.mini_window.quit_requested.connect(self._on_mini_quit)

        self.portrait_label.middle_clicked.connect(self.toggle_mini_mode)
        self.portrait_label.double_clicked.connect(self.toggle_mini_mode)
        self.portrait_label.context_menu_requested.connect(self._on_portrait_menu)

    def toggle_mini_mode(self):
        """聊天模式 ↔ 迷你模式互斥切换。"""
        if self.mini_window.isVisible():
            self.show_chat_mode()
        else:
            self.show_mini_mode()

    def show_mini_mode(self):
        """进入迷你模式：迷你窗口定位右下角并显示，聊天窗口隐藏。
        聊天线程/浏览器池保持运行，切回无延迟。"""
        self.mini_window.move_to_bottom_right()
        self.mini_window.show()
        self.hide()

    def show_chat_mode(self):
        """回到聊天模式：隐藏迷你窗口，恢复聊天窗口并置前。"""
        self.mini_window.hide()
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_portrait_menu(self, pos):
        """立绘右键菜单：进入迷你模式 / 退出（迷你模式时迷你窗口自带菜单）。"""
        from PySide6.QtWidgets import QAction, QMenu
        menu = QMenu(self)
        mini_action = QAction("进入迷你模式", self)
        mini_action.triggered.connect(self.show_mini_mode)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._on_mini_quit)
        menu.addAction(mini_action)
        menu.addAction(quit_action)
        menu.exec(pos)

    def _on_mini_quit(self):
        """迷你窗口/立绘菜单"退出"：走主窗口关闭流程（清理线程/历史/浏览器池）后退出。"""
        self.close()
        QApplication.quit()

    @staticmethod
    def _restore_history() -> list:
        """恢复上次会话历史；确保系统提示词为第一条且为最新版本。"""
        loaded = load_history()
        if loaded and loaded[0].get("role") == "system":
            loaded[0]["content"] = SYSTEM_PROMPT  # 提示词以当前版本为准
            return loaded
        return [{"role": "system", "content": SYSTEM_PROMPT}] + [
            m for m in loaded if m.get("role") != "system"
        ]

    # ===== 界面构建 =====
    def init_ui(self):
        central_widget = QWidget()
        central_widget.setStyleSheet("background: transparent;")
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 5)
        main_layout.setSpacing(0)

        # ----- 左侧：立绘 -----
        self.portrait_container = QWidget()
        self.portrait_container.setFixedWidth(PORTRAIT_DISPLAY_WIDTH + 10)
        self.portrait_container.setStyleSheet("background: transparent;")
        portrait_layout = QVBoxLayout(self.portrait_container)
        portrait_layout.setContentsMargins(5, 35, 5, 5)
        portrait_layout.setAlignment(Qt.AlignTop)

        self.portrait_label = DraggablePortrait(self)
        self.portrait_label.setFixedWidth(PORTRAIT_DISPLAY_WIDTH)
        self.portrait_label.setMinimumHeight(300)
        portrait_layout.addWidget(self.portrait_label)

        self.mood_label = QLabel("💙 默认")
        self.mood_label.setAlignment(Qt.AlignCenter)
        self.mood_label.setStyleSheet(
            "color: #555; font-size: 10px; padding: 2px; background: transparent;"
        )
        portrait_layout.addWidget(self.mood_label)

        main_layout.addWidget(self.portrait_container)

        # ----- 右侧：聊天 -----
        right_container = QWidget()
        right_container.setStyleSheet("background: transparent;")
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 5, 5)
        right_layout.setSpacing(3)

        self.title_bar = TitleBar(self)
        right_layout.addWidget(self.title_bar)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(SCROLL_AREA_STYLE)
        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_layout.setSpacing(5)
        self.chat_layout.setContentsMargins(3, 3, 3, 3)
        self.scroll_area.setWidget(self.chat_container)
        right_layout.addWidget(self.scroll_area)

        # 底部输入
        input_layout = QHBoxLayout()
        input_layout.setSpacing(5)

        self.input_text = QTextEdit()
        self.input_text.setFixedHeight(30)
        self.input_text.setStyleSheet(INPUT_STYLE)
        self.input_text.setPlaceholderText("输入... (Ctrl+Enter)")
        self.input_text.setFont(QFont("Microsoft YaHei", 9))
        self.input_text.installEventFilter(self)
        # 拖拽图片交给窗口统一处理（自动填入 /image 路径）
        self.input_text.setAcceptDrops(False)
        input_layout.addWidget(self.input_text)

        self.select_image_btn = QPushButton("📎")
        self.select_image_btn.setFixedSize(30, 30)
        self.select_image_btn.setStyleSheet(IMAGE_BTN_STYLE)
        self.select_image_btn.clicked.connect(self.on_select_image)
        input_layout.addWidget(self.select_image_btn)

        self.stop_btn = QPushButton("⏹")
        self.stop_btn.setFixedSize(30, 30)
        self.stop_btn.setToolTip("停止生成")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(STOP_BTN_STYLE)
        self.stop_btn.clicked.connect(self.on_stop_clicked)
        input_layout.addWidget(self.stop_btn)

        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedSize(40, 30)
        self.send_btn.setStyleSheet(SEND_BTN_STYLE)
        self.send_btn.clicked.connect(self.on_send_clicked)
        input_layout.addWidget(self.send_btn)

        right_layout.addLayout(input_layout)
        main_layout.addWidget(right_container)

        # 窗口级拖拽（图片）
        self.setAcceptDrops(True)

    # ===== 立绘切换（异步加载，不阻塞主线程） =====
    def update_portrait(self, mood: str = "默认"):
        emoji = MOOD_EMOJI_MAP.get(mood, "🐳")
        self.mood_label.setText(f"{emoji} {mood}")
        portrait_path = get_portrait_path(mood, PORTRAIT_FOLDER)
        if not os.path.exists(portrait_path):
            self.portrait_label.setText(f"⚠️\n找不到\n{os.path.basename(portrait_path)}")
            return
        self._portrait_seq = self._portrait_loader.submit(portrait_path)

    def _on_portrait_loaded(self, req_id: int, pixmap: QPixmap):
        if req_id != self._portrait_seq:
            return  # 过期请求，忽略
        self.portrait_label.setPixmap(pixmap)
        self.portrait_label.setText("")

    def _on_portrait_failed(self, req_id: int, reason: str):
        if req_id != self._portrait_seq:
            return
        self.portrait_label.setText(f"⚠️\n加载失败\n{reason[:20]}")

    # ===== 拖拽图片 =====
    def dragEnterEvent(self, event):
        if self._drag_has_image(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._first_dropped_image(event.mimeData())
        if path:
            self._fill_image_command(path)
            event.acceptProposedAction()

    def _drag_has_image(self, mime) -> bool:
        return any(
            u.isLocalFile() and u.toLocalFile().lower().endswith(IMAGE_EXTENSIONS)
            for u in mime.urls()
        )

    def _first_dropped_image(self, mime) -> str | None:
        for url in mime.urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path.lower().endswith(IMAGE_EXTENSIONS):
                    return path
        return None

    # ===== 停止生成 =====
    def on_stop_clicked(self):
        if self.worker:
            self.worker.stop_flag = True
        self.stop_btn.setEnabled(False)
        logger.info("用户点击停止生成")

    # ===== 事件过滤器 =====
    def eventFilter(self, obj, event):
        if obj is self.input_text and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Return and (event.modifiers() & Qt.ControlModifier):
                self.on_send_clicked()
                return True
        return super().eventFilter(obj, event)

    # ===== 选择图片 =====
    def on_select_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        )
        if file_path:
            self._fill_image_command(file_path)

    def _fill_image_command(self, path: str):
        self.input_text.setText(f'/image {path} ')
        self.input_text.setFocus()
        cursor = self.input_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.input_text.setTextCursor(cursor)

    # ===== 发送消息 =====
    def on_send_clicked(self):
        raw_input = self.input_text.toPlainText().strip()
        if not raw_input:
            return

        # 上一条命令仍在执行（含批量/流式中）→ 拦截，避免覆盖线程引用导致闪退
        if self.thread is not None and self.thread.isRunning():
            self.add_message("系统", "🐳 鲸鲸正在忙上一件事，等它说完再发哦～", align_left=True)
            return

        self.input_text.clear()

        # 主人回复了 → 重置空闲撩人计数（下一次空闲期重新计算）
        self._proactive_count = 0

        now = time.time()
        delta = now - self.last_reply_time
        if delta > INACTIVE_GAP_SECONDS:
            minutes = int(delta // 60)
            raw_input = f"（距上次回复已过去{minutes}分钟）{raw_input}"

        if raw_input.lower().startswith("/image"):
            cmd = raw_input[len("/image"):].strip()
            image_path, question = process_image_command(cmd)
            if not image_path:
                self.add_message("系统", "⚠️ 用法：/image <图片路径> [可选问题]", align_left=True)
                return
            self.add_message("我", f"{question} [图片]", align_left=False)
            self.start_worker(is_image=True, image_path=image_path, question=question)
        else:
            self.add_message("我", raw_input, align_left=False)
            self.start_worker(is_image=False, text=raw_input)

    # ===== 启动工作线程 =====
    def start_worker(self, is_image, text=None, image_path=None, question=None):
        self.show_typing()

        self.thread = QThread()
        self.worker = ChatWorker(
            self.history, self.client, self.browser_pool,
            reminder_manager=self.reminder_manager,
        )
        self.worker.moveToThread(self.thread)

        self.worker.chunk_received.connect(self.on_chunk_received)
        self.worker.mood_changed.connect(self.on_mood_changed)
        self.worker.finished.connect(self.on_finished)
        self.worker.error_occurred.connect(self.on_error)

        if is_image:
            # 必须 DirectConnection：lambda 没有 QObject 接收者，默认会被 PySide6
            # 排队回主线程执行 → API 请求阻塞主线程 → 界面冻结/未响应（探针实测证实）。
            # DirectConnection 让 lambda 在 QThread 线程内直接执行。
            self.thread.started.connect(
                lambda: self.worker.send_image(image_path, question),
                Qt.ConnectionType.DirectConnection,
            )
        else:
            self.thread.started.connect(
                lambda: self.worker.send_text(text),
                Qt.ConnectionType.DirectConnection,
            )

        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.worker.deleteLater)  # 线程结束时安全回收 worker
        self.stop_btn.setEnabled(True)
        self.thread.start()

    # ===== 接收流式块（打字机效果） =====
    def on_chunk_received(self, chunk):
        # 移除“正在思考”标签
        if self.typing_label:
            self.chat_layout.removeWidget(self.typing_label)
            self.typing_label.deleteLater()
            self.typing_label = None

        # 创建气泡（如果还没有）
        if self.current_bubble is None:
            self.current_bubble = self.create_bubble("鲸鲸", "", align_left=True, is_streaming=True)
            self.chat_layout.addWidget(self.current_bubble)

        # 将新 chunk 拆成字符入队
        self.char_queue.extend(chunk)

        # 启动定时器（如果未启动）
        if not self.typing_timer.isActive():
            self.typing_timer.start()

    def on_mood_changed(self, mood):
        """Worker 在流式过程中实时报告情绪，UI 只负责换立绘。"""
        self.update_portrait(mood)

    # ===== 定时器逐字输出 =====
    def type_next_char(self):
        if not self.char_queue:
            self.typing_timer.stop()
            if self.pending_finish:
                # 走批量感知收尾：批量中间只轻量重置，最后一条才回收线程
                self._finish_message(self._pending_remaining)
            return

        char = self.char_queue.popleft()
        label = self.current_bubble.findChild(QLabel)
        if label:
            # 逐字拼接纯文本，渲染时转富文本（URL 可点击）
            plain = getattr(label, "_plain", "") + char
            label._plain = plain
            label.setText(plain_to_html(plain))
            self.scroll_to_bottom()

    # ===== 回复完成（延迟处理） =====
    def on_finished(self, full_reply, remaining):
        # remaining 由 worker 在发射时定格（本条完成后的剩余命令数），
        # 不再跨线程读 worker.batch_remaining（竞态会导致提前回收线程 → worker 被删闪退）
        self.pending_finish = True
        self._pending_remaining = remaining
        self.stop_btn.setEnabled(False)
        # 如果队列已空且定时器已停，立即收尾
        if not self.char_queue and not self.typing_timer.isActive():
            self._finish_message(remaining)

    def _finish_message(self, remaining):
        """
        单条消息收尾：批量命令（多任务/模型多命令）未结束时只重置消息状态、
        保留线程；最后一条才完整收尾（回收线程）。
        """
        if remaining > 0:
            self._reset_message_state()  # 批量中间：轻量重置，线程继续跑下一条
        else:
            self.do_finish_actions()

    def _reset_message_state(self):
        """重置当前消息的 UI 状态（气泡/打字机/思考标签/停止按钮）。"""
        self.current_bubble = None
        self.pending_finish = False
        self.char_queue.clear()
        if self.typing_timer.isActive():
            self.typing_timer.stop()
        self.hide_typing()  # 无论是否有输出，都必须清掉"正在思考"标签
        self.stop_btn.setEnabled(False)

    # ===== 真正的收尾操作 =====
    def do_finish_actions(self):
        self._reset_message_state()
        self.last_reply_time = time.time()
        self._stop_thread()
        self.scroll_to_bottom()

    # ===== 错误处理 =====
    def on_error(self, error_msg):
        logger.warning("聊天出错：%s", error_msg)
        self.add_message("系统", f"❌ 错误：{error_msg}", align_left=True)
        self._stop_thread()
        self._reset_message_state()
        self.scroll_to_bottom()

    # ===== 线程清理 =====
    def _stop_thread(self):
        """
        停止并回收工作线程（非阻塞）：
        1) 只 quit 不 wait——避免主线程卡死（窗口拖不动/回复闪现的根因）；
        2) thread/worker 的 Python 引用暂存到 _retired，等线程 finished 后再释放——
           否则 GC 会在 QThread 退出前析构对象（"QThread: Destroyed while thread
           is still running"），真机表现为闪退；
        3) 对象删除统一交给 finished → deleteLater。
        """
        if self.thread is not None:
            if self.thread.isRunning():
                self.thread.quit()
            self._retire(self.thread, self.worker)
        self.thread = None
        self.worker = None

    def _retire(self, thread, worker):
        """暂存待回收对象引用；线程结束后（finished）自动移除。"""
        pair = (thread, worker)
        self._retired.append(pair)

        def _cleanup():
            try:
                self._retired.remove(pair)
            except ValueError:
                pass

        thread.finished.connect(_cleanup)

    # ===== 气泡 =====
    def create_bubble(self, sender, text, align_left, is_streaming=False):
        bubble_widget = QWidget()
        bubble_widget.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(bubble_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        bubble = QFrame()
        bubble.setStyleSheet(BUBBLE_STYLE)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(3, 3, 3, 3)

        display_text = f"{sender}：{text}" if not is_streaming else text
        label = QLabel(plain_to_html(display_text) if not is_streaming else "")
        label._plain = display_text  # 纯文本备份（打字机逐字拼接用）
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)          # 富文本：URL 可点击
        label.setOpenExternalLinks(True)          # 点击链接用默认浏览器打开
        label.setFont(QFont("Microsoft YaHei", 9))
        label.setStyleSheet("color: #1A1A1A; background: transparent;")
        bubble_layout.addWidget(label)
        bubble.setLayout(bubble_layout)

        if align_left:
            layout.addWidget(bubble)
            layout.addStretch()
        else:
            layout.addStretch()
            layout.addWidget(bubble)

        return bubble_widget

    def add_message(self, sender, text, align_left=True):
        bubble_widget = self.create_bubble(sender, text, align_left, is_streaming=False)
        self.chat_layout.addWidget(bubble_widget)
        self.scroll_to_bottom()

    # ===== 打字状态 =====
    def show_typing(self):
        if self.typing_label is None:
            self.typing_label = QLabel("🐳 鲸鲸正在思考...")
            self.typing_label.setStyleSheet(TYPING_LABEL_STYLE)
            self.chat_layout.addWidget(self.typing_label)
            self.scroll_to_bottom()

    def hide_typing(self):
        if self.typing_label:
            self.chat_layout.removeWidget(self.typing_label)
            self.typing_label.deleteLater()
            self.typing_label = None

    # ===== 滚动到底部 =====
    def scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ===== 关闭事件 =====
    def closeEvent(self, event):
        thread = self.thread
        self._stop_thread()
        if thread is not None:
            # 等工作线程自然退出（最多 3 秒，正常情况立即返回）；
            # 网络卡死等异常由 client timeout 兜底，避免进程退出时线程仍在运行 → 闪退
            thread.wait(3000)
        self._proactive_timer.stop()       # 停止空闲撩人定时器
        self.typing_timer.stop()           # 停止打字机定时器
        self.browser_pool.close()          # 关闭残留的搜索浏览器实例
        self._portrait_loader.stop()       # 停止立绘加载线程
        self._portrait_loader.wait(3000)
        save_history(self.history)         # 持久化对话历史，跨会话恢复
        self.reminder_manager.shutdown()   # 取消未触发的提醒
        self.mini_window.close()           # 关闭迷你窗口（若存在）
        logger.info("窗口关闭，历史已保存（共 %d 条）", len(self.history))
        event.accept()
