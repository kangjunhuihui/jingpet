# reminder.py
# 定时提醒：解析"10分钟后提醒我喝水"类命令 → 倒计时 → 到点发 reminder_fired 信号。
# 用 threading.Timer（不依赖 Qt 事件循环，Worker 线程内也可调度）；
# 信号经 Qt 队列连接跨线程送达主窗口。

import logging
import re
import threading

from PySide6.QtCore import QObject, Signal

from config import REMINDER_AFTER_PATTERN, REMINDER_LEAD_PATTERN

logger = logging.getLogger("jingjing.reminder")

DEFAULT_REMINDER_CONTENT = "时间到啦～"


def parse_reminder(text: str) -> tuple[int, str] | None:
    """
    解析提醒命令，返回 (分钟数, 提醒内容)；非提醒命令返回 None。
    支持两种语序："10分钟后提醒我喝水" / "提醒我10分钟后喝水"；
    时间单位支持 分钟/小时（缺省按分钟）。
    """
    stripped = text.strip()
    for pattern in (REMINDER_AFTER_PATTERN, REMINDER_LEAD_PATTERN):
        match = re.match(pattern, stripped)
        if not match:
            continue
        number = int(match.group(1))
        # 看数字后两个字符是否为"小时"
        pos = stripped.find(match.group(1))
        unit_text = stripped[pos + len(match.group(1)): pos + len(match.group(1)) + 2]
        minutes = number * (60 if unit_text == "小时" else 1)
        content = (match.group(2) or "").strip()
        return minutes, content or DEFAULT_REMINDER_CONTENT
    return None


class ReminderManager(QObject):
    """定时提醒管理器：schedule() 安排提醒，到点发 reminder_fired 信号。"""

    reminder_fired = Signal(str)  # 参数：提醒内容

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timers = {}  # id -> threading.Timer

    def schedule(self, minutes: float, content: str) -> int:
        """安排一个提醒，返回提醒 ID。分钟数 <= 0 视为立即触发。"""
        reminder_id = max(self._timers, default=0) + 1
        seconds = max(0.0, minutes * 60)

        def _fire():
            self._timers.pop(reminder_id, None)
            logger.info("提醒触发：%s", content)
            self.reminder_fired.emit(content)

        timer = threading.Timer(seconds, _fire)
        timer.daemon = True
        self._timers[reminder_id] = timer
        timer.start()
        logger.info("已安排提醒（id=%d, %.0f 分钟后）：%s", reminder_id, minutes, content)
        return reminder_id

    def cancel(self, reminder_id: int) -> None:
        """取消提醒（幂等）。"""
        timer = self._timers.pop(reminder_id, None)
        if timer is not None:
            timer.cancel()

    def pending_count(self) -> int:
        return len(self._timers)

    def shutdown(self) -> None:
        """取消全部提醒（窗口关闭时调用）。"""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
