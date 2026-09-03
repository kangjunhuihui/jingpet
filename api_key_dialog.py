# api_key_dialog.py
# 登录界面：聊天前先输入 API Key（支持测试连接），并保存本地设置

import logging

from openai import OpenAI
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from config import BASE_URL, TEXT_MODEL, VISION_MODEL
from settings import load_settings, save_settings
from widgets import TitleBar

logger = logging.getLogger("jingjing.api_key_dialog")

# ---------- 样式常量 ----------
FIELD_STYLE = """
    QLineEdit {
        background-color: #F5F7FA;
        border: 1px solid #DDDDDD;
        border-radius: 8px;
        padding: 8px 10px;
        font-size: 12px;
    }
    QLineEdit:focus { border-color: #007AFF; background-color: #FFFFFF; }
"""

PRIMARY_BTN_STYLE = """
    QPushButton {
        background-color: #007AFF;
        color: white;
        border: none;
        border-radius: 8px;
        padding: 9px 0;
        font-size: 13px;
        font-weight: bold;
    }
    QPushButton:hover { background-color: #0055CC; }
    QPushButton:pressed { background-color: #003D99; }
"""

SECONDARY_BTN_STYLE = """
    QPushButton {
        background-color: #F0F2F5;
        color: #333333;
        border: 1px solid #DDDDDD;
        border-radius: 8px;
        padding: 8px 0;
        font-size: 12px;
    }
    QPushButton:hover { background-color: #E3E6EA; }
"""

STATUS_OK_STYLE = "font-size: 10px; color: #1DB954; background: transparent;"
STATUS_ERROR_STYLE = "font-size: 10px; color: #E5484D; background: transparent;"
STATUS_INFO_STYLE = "font-size: 10px; color: #888888; background: transparent;"


class ConnectionTester(QThread):
    """后台测试 API 连通性，避免阻塞界面"""

    result_ready = Signal(bool, str)

    def __init__(self, api_key, base_url, model, parent=None):
        super().__init__(parent)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def run(self):
        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=15)
            client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "你好"}],
                max_tokens=1,
            )
            logger.info("连接测试成功（model=%s）", self.model)
            self.result_ready.emit(True, "连接成功，可以开始聊天啦～")
        except Exception as e:
            logger.warning("连接测试失败（model=%s）：%s", self.model, str(e)[:200])
            self.result_ready.emit(False, f"连接失败：{str(e)[:100]}")


class ApiKeyDialog(QDialog):
    """无边框圆角登录卡片：API Key + 接口地址 + 模型 + 测试连接"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = load_settings()
        self._tester = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(420, 580)

        card = QWidget(self)
        card.setObjectName("loginCard")
        card.setStyleSheet("#loginCard { background-color: #FFFFFF; border-radius: 16px; }")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 0, 24, 24)
        layout.setSpacing(10)

        # 标题栏（可拖动 + 关闭）
        title_bar = TitleBar("🐳 鲸鲸 · 登录", card)
        title_bar.setStyleSheet(
            "background-color: #007AFF;"
            "border-top-left-radius: 16px;"
            "border-top-right-radius: 16px;"
        )
        title_bar.close_requested.connect(self.reject)
        layout.addWidget(title_bar)

        # 欢迎区
        emoji_label = QLabel("🐳")
        emoji_label.setAlignment(Qt.AlignCenter)
        emoji_label.setStyleSheet("font-size: 52px; background: transparent;")
        layout.addWidget(emoji_label)

        welcome_label = QLabel("欢迎回来，主人～")
        welcome_label.setAlignment(Qt.AlignCenter)
        welcome_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #1A1A1A; background: transparent;"
        )
        layout.addWidget(welcome_label)

        subtitle_label = QLabel("先输入 API Key，才能开始和鲸鲸聊天哦")
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setStyleSheet("font-size: 11px; color: #999999; background: transparent;")
        layout.addWidget(subtitle_label)
        layout.addSpacing(4)

        # API Key（不预填、不保存，每次启动手动输入）
        layout.addWidget(self._field_label("API Key"))
        self.key_edit = QLineEdit("")
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk- 开头的密钥")
        self.key_edit.setStyleSheet(FIELD_STYLE)
        self.key_edit.returnPressed.connect(self.on_start_chat)
        layout.addWidget(self.key_edit)

        self.show_key_check = QCheckBox("显示密钥")
        self.show_key_check.toggled.connect(self._toggle_key_visible)
        self.show_key_check.setStyleSheet(
            "color: #666666; font-size: 10px; background: transparent;"
        )
        layout.addWidget(self.show_key_check)

        # 接口地址
        layout.addWidget(self._field_label("接口地址（Base URL）"))
        self.url_edit = QLineEdit(self._settings["base_url"])
        self.url_edit.setStyleSheet(FIELD_STYLE)
        layout.addWidget(self.url_edit)

        # 模型（左右两栏）
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        text_col = QVBoxLayout()
        vision_col = QVBoxLayout()

        text_col.addWidget(self._field_label("文本模型"))
        self.text_model_edit = QLineEdit(self._settings["text_model"])
        self.text_model_edit.setStyleSheet(FIELD_STYLE)
        text_col.addWidget(self.text_model_edit)

        vision_col.addWidget(self._field_label("视觉模型"))
        self.vision_model_edit = QLineEdit(self._settings["vision_model"])
        self.vision_model_edit.setStyleSheet(FIELD_STYLE)
        vision_col.addWidget(self.vision_model_edit)

        model_row.addLayout(text_col)
        model_row.addLayout(vision_col)
        layout.addLayout(model_row)

        # 状态提示
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(STATUS_INFO_STYLE)
        layout.addWidget(self.status_label)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.test_btn = QPushButton("测试连接")
        self.test_btn.setStyleSheet(SECONDARY_BTN_STYLE)
        self.test_btn.setCursor(Qt.PointingHandCursor)
        self.test_btn.clicked.connect(self.on_test_connection)
        btn_row.addWidget(self.test_btn)

        self.start_btn = QPushButton("开始聊天")
        self.start_btn.setStyleSheet(PRIMARY_BTN_STYLE)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.on_start_chat)
        btn_row.addWidget(self.start_btn)

        layout.addLayout(btn_row)
        layout.addStretch()

    # ---------- 内部工具 ----------
    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 11px; color: #555555; background: transparent;")
        return label

    def _toggle_key_visible(self, checked: bool):
        self.key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _collect_settings(self) -> dict:
        """收集当前表单内容（空值回退到默认配置）"""
        return {
            "api_key": self.key_edit.text().strip(),
            "base_url": self.url_edit.text().strip() or BASE_URL,
            "text_model": self.text_model_edit.text().strip() or TEXT_MODEL,
            "vision_model": self.vision_model_edit.text().strip() or VISION_MODEL,
        }

    def get_settings(self) -> dict:
        """返回确认后生效的设置（供主程序创建客户端）"""
        return dict(self._settings)

    def _show_status(self, text: str, style: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(style)

    # ---------- 按钮逻辑 ----------
    def on_test_connection(self):
        if self._tester and self._tester.isRunning():
            return
        settings = self._collect_settings()
        if not settings["api_key"]:
            self._show_status("主人～ 先把 API Key 填上才能测试哦", STATUS_ERROR_STYLE)
            return
        self.test_btn.setEnabled(False)
        self._show_status("正在连接 DeepSeek……", STATUS_INFO_STYLE)
        self._tester = ConnectionTester(
            settings["api_key"], settings["base_url"], settings["text_model"], self
        )
        self._tester.result_ready.connect(self._on_test_result)
        self._tester.start()

    def _on_test_result(self, ok: bool, message: str):
        self.test_btn.setEnabled(True)
        self._show_status(message, STATUS_OK_STYLE if ok else STATUS_ERROR_STYLE)

    def closeEvent(self, event):
        """关闭时等待测试线程结束，避免 QThread 运行中被析构。"""
        if self._tester is not None and self._tester.isRunning():
            self._tester.wait(2000)
        event.accept()

    def on_start_chat(self):
        settings = self._collect_settings()
        if not settings["api_key"]:
            self._show_status("主人～ 先把 API Key 填上才能开始聊天哦", STATUS_ERROR_STYLE)
            return
        self._settings = settings
        save_settings(settings)
        logger.info("登录确认，设置已保存")
        self.accept()
