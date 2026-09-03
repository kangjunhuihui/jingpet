# widgets.py
# 可复用控件：可拖动窗口的标题栏、可拖动的立绘标签

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class TitleBar(QWidget):
    """可拖动窗口的标题栏（支持可选标题与关闭按钮）"""

    close_requested = Signal()

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setStyleSheet("background-color: #007AFF; border-radius: 0px;")
        self.drag_pos = None

        if title:
            bar_layout = QHBoxLayout(self)
            bar_layout.setContentsMargins(12, 0, 6, 0)
            bar_layout.setSpacing(6)

            title_label = QLabel(title)
            title_label.setStyleSheet(
                "color: white; background: transparent; font-size: 11px; font-weight: bold;"
            )
            bar_layout.addWidget(title_label)
            bar_layout.addStretch()

            close_btn = QPushButton("✕")
            close_btn.setFixedSize(20, 20)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.setStyleSheet(
                "QPushButton { background: transparent; color: white; border: none; font-size: 12px; }"
                "QPushButton:hover { background: rgba(255,255,255,0.25); border-radius: 10px; }"
            )
            close_btn.clicked.connect(self.close_requested)
            bar_layout.addWidget(close_btn)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.drag_pos is not None:
            top_level = self.window()
            if top_level:
                delta = event.globalPosition().toPoint() - self.drag_pos
                top_level.move(top_level.pos() + delta)
                self.drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self.drag_pos = None
        event.accept()


class DraggablePortrait(QLabel):
    """可拖动的立绘标签（左键拖动窗口；中键/双击触发迷你模式切换；右键弹菜单）"""

    middle_clicked = Signal()          # 鼠标中键点击
    double_clicked = Signal()          # 左键双击
    context_menu_requested = Signal(object)  # 右键菜单（参数：全局坐标 QPoint）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.drag_pos = None
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background: transparent; border: none;")
        self.setMinimumSize(100, 150)
        self.setText("🐳\n加载中...")
        self.setFont(QFont("Microsoft YaHei", 12))

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
            event.accept()
        elif event.button() == Qt.MiddleButton:
            self.middle_clicked.emit()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()
            event.accept()

    def contextMenuEvent(self, event):
        self.context_menu_requested.emit(event.globalPos())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.drag_pos is not None:
            parent_window = self.window()
            if parent_window:
                delta = event.globalPosition().toPoint() - self.drag_pos
                parent_window.move(parent_window.pos() + delta)
                self.drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self.drag_pos = None
        event.accept()
