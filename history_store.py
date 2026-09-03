# history_store.py
# 对话历史持久化：保存到用户主目录，跨会话恢复上下文
# 保存时裁剪：保留系统提示词 + 最近 MAX_HISTORY_ENTRIES 条，防止文件无限增长

import json
import os

HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".jingjing_history.json")

VALID_ROLES = {"system", "user", "assistant"}

# 历史条数上限（含系统提示词）；超出丢弃最旧的
MAX_HISTORY_ENTRIES = 200


def load_history() -> list:
    """读取历史；文件缺失 / 损坏 / 格式非法时返回空列表"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            cleaned = [
                m for m in data
                if isinstance(m, dict)
                and m.get("role") in VALID_ROLES
                and isinstance(m.get("content"), str)
            ]
            if cleaned:
                return cleaned
    except (OSError, ValueError):
        pass
    return []


def save_history(history: list) -> None:
    """写入历史（静默忽略写入失败）；超过上限时裁剪最旧的非系统消息。"""
    data = trim_history(history)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def trim_history(history: list) -> list:
    """裁剪历史：保留系统提示词（若为首条）+ 最近 MAX_HISTORY_ENTRIES 条。"""
    if len(history) <= MAX_HISTORY_ENTRIES:
        return history
    if history and history[0].get("role") == "system":
        return [history[0]] + history[-(MAX_HISTORY_ENTRIES - 1):]
    return history[-MAX_HISTORY_ENTRIES:]
