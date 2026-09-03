# app.py
# 程序启动入口：初始化日志 → 登录界面输入 API Key → 进入聊天主窗口
# 支持 --smoke <结果文件>：打包自检模式（离屏构建界面后退出，供打包验证用）

import logging
import os
import sys
import tempfile
import time

from openai import OpenAI
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QSharedMemory
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from api_key_dialog import ApiKeyDialog
from log_config import setup_logging
from main_window import MainWindow

logger = logging.getLogger("jingjing.app")


def _run_smoke(result_file: str) -> int:
    """打包自检：离屏构建登录界面与主窗口，验证立绘可加载，结果写入文件。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv)
    checks = []
    try:
        dlg = ApiKeyDialog()
        checks.append(("login_dialog", True))
        dlg.close()

        w = MainWindow(client=object())
        w.show()
        app.processEvents()
        checks.append(("main_window", True))

        w.update_portrait("开心")
        deadline = time.time() + 5
        while time.time() < deadline and w.portrait_label.pixmap() is None:
            app.processEvents()
            time.sleep(0.02)
        checks.append(("portrait_loaded", w.portrait_label.pixmap() is not None))

        # 迷你模式图标（jing.png 随 Assets 打包，验证包内资源完整）
        checks.append(("mini_icon_loaded", w.mini_window.icon_label.pixmap() is not None))
        w.close()
        app.processEvents()

        # selenium 浏览器子模块是惰性导入，打包时易漏（表现为搜索报 ModuleNotFoundError）
        try:
            import importlib
            for mod in (
                "selenium.webdriver.edge.webdriver",
                "selenium.webdriver.edge.service",
                "selenium.webdriver.chrome.webdriver",
                "selenium.webdriver.chrome.options",
                "selenium.webdriver.firefox.webdriver",
                "selenium.webdriver.firefox.options",
            ):
                importlib.import_module(mod)
            checks.append(("selenium_modules", True))
        except Exception as e:
            checks.append(("selenium_modules", repr(e)[:100]))

        # 新建文件模块（惰性导入易漏，打包验证用；纯路径归一化，不触碰文件系统）
        try:
            from file_creator import normalize_create_path
            _p, _err = normalize_create_path("E:/jingjing_smoke_check.txt")
            checks.append(("file_creator_module", _p is not None and _err is None))
        except Exception as e:
            checks.append(("file_creator_module", repr(e)[:100]))
    except Exception as e:
        checks.append(("exception", repr(e)))

    ok = all(result for _, result in checks)
    with open(result_file, "w", encoding="utf-8") as f:
        for name, result in checks:
            f.write(f"{name}: {'OK' if result else 'FAIL'}\n")
        f.write(f"overall: {'OK' if ok else 'FAIL'}\n")
    return 0 if ok else 1


def main() -> int:
    setup_logging()
    logger.info("鲸鲸启动（Python %s, PySide6 %s）", sys.version.split()[0], pyside_version)

    # 打包自检模式：跳过正常流程，验证后立即退出
    if "--smoke" in sys.argv:
        idx = sys.argv.index("--smoke")
        result_file = (
            sys.argv[idx + 1]
            if len(sys.argv) > idx + 1
            else os.path.join(tempfile.gettempdir(), "jingjing_smoke.txt")
        )
        return _run_smoke(result_file)

    app = QApplication(sys.argv)
    app.setApplicationName("鲸鲸")
    app.setFont(QFont("Microsoft YaHei", 9))

    # 单实例锁：已有鲸鲸在运行则提示退出（防止双实例互相覆盖历史/日志）
    singleton = QSharedMemory("jingjing_singleton_lock")
    if not singleton.create(1):
        logger.warning("检测到已有鲸鲸实例在运行")
        QMessageBox.information(None, "鲸鲸", "鲸鲸已经在运行啦～ 去任务栏找找它吧！")
        return 0

    # 第一步：登录（输入 API Key）
    login = ApiKeyDialog()
    if login.exec() != ApiKeyDialog.Accepted:
        logger.info("用户取消登录，退出")
        return 0
    logger.info("登录成功，进入聊天界面")

    # 第二步：用确认的设置创建客户端，进入聊天主窗口
    settings = login.get_settings()
    # timeout=60：API 无响应时 worker 线程在 60s 内报错（默认 600s 会让关窗时
    # 线程仍在运行 → 退出闪退，且 UI 无限转圈无反馈）
    client = OpenAI(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        timeout=60,
    )

    window = MainWindow(client=client)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
