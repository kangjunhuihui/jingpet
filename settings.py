# settings.py
# 本地设置持久化：接口地址、模型名
# 注意：API Key 一律不落盘——登录版每次启动手动输入；
# 个人版（免登录）：API Key 从与程序同级的 APIkey.txt 读取（load_api_key 两版共用，
# 登录版不调用即可，无需来回切换源码）

import json
import os
import sys

from config import BASE_URL, TEXT_MODEL, VISION_MODEL

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".jingjing_settings.json")

DEFAULT_SETTINGS = {
    "base_url": BASE_URL,
    "text_model": TEXT_MODEL,
    "vision_model": VISION_MODEL,
}


def _app_dir() -> str:
    """程序所在目录：打包后为 exe 目录（onefile 的 _MEIPASS 是临时解包区，不可用）；
    开发/源码运行为项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# API Key 文件：与程序同级（个人版部署时直接改 C 盘根目录的 APIkey.txt 即换 key，无需重打包）
KEY_FILE = os.path.join(_app_dir(), "APIkey.txt")


def load_api_key() -> str | None:
    """从与程序同级的 APIkey.txt 读取 API Key（strip 后返回）；
    文件缺失 / 为空 / 读取失败返回 None。"""
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
        return key or None
    except OSError:
        return None


def load_settings() -> dict:
    """读取本地设置；文件不存在或损坏时返回默认值"""
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    """写入本地设置；只保存白名单字段，API Key 永不落盘（静默忽略写入失败）"""
    data = {k: v for k, v in settings.items() if k in DEFAULT_SETTINGS}
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
