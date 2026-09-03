# mini_window.py
# 迷你鲸鱼窗口：鼠标中键/双击立绘切换出的 150×150 小图标。
# 透明无边框、置顶、整窗可拖动（左键）；中键/双击返回聊天；右键菜单（返回聊天 / 退出）。
# 每次进入定位到鼠标所在屏幕（或主屏）右下角（用户拍板：不记忆拖动位置）。

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QVBoxLayout, QWidget

from config import MINI_ICON_PATH, MINI_MARGIN, MINI_SIZE

logger = logging.getLogger("jingjing.mini_window")


class MiniIconWindow(QWidget):
    """迷你鲸鱼图标窗口。中键/双击 → back_requested；右键菜单 → back/quit。"""

    back_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.drag_pos = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(MINI_SIZE, MINI_SIZE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setStyleSheet("background: transparent;")
        layout.addWidget(self.icon_label)

        self._load_icon()

    def _load_icon(self):
        """加载迷你图标（缩放至 150×150）；文件缺失时用 🐳 占位。"""
        pixmap = QPixmap(MINI_ICON_PATH)
        if pixmap.isNull():
            logger.warning("迷你图标加载失败：%s（使用 🐳 占位）", MINI_ICON_PATH)
            self.icon_label.setText("🐳")
            self.icon_label.setStyleSheet("font-size: 64px; background: transparent;")
        else:
            scaled = pixmap.scaled(
                MINI_SIZE, MINI_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.icon_label.setPixmap(scaled)

    def move_to_bottom_right(self):
        """定位到鼠标所在屏幕（或主屏）的右下角，留 MINI_MARGIN 边距。"""
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(
            geo.x() + geo.width() - MINI_SIZE - MINI_MARGIN,
            geo.y() + geo.height() - MINI_SIZE - MINI_MARGIN,
        )

    # ---------- 交互：左键拖动，中键/双击返回，右键菜单 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
            event.accept()
        elif event.button() == Qt.MiddleButton:
            self.back_requested.emit()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_pos is not None:
            delta = event.globalPosition().toPoint() - self.drag_pos
            self.move(self.pos() + delta)
            self.drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.back_requested.emit()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        back_action = QAction("返回聊天", self)
        back_action.triggered.connect(self.back_requested)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(back_action)
        menu.addAction(quit_action)
        menu.exec(event.globalPos())
