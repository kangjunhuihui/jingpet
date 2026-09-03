# log_config.py
# 日志配置：控制台输出 + 滚动文件日志（~/.jingjing/jingjing.log，用户目录）

import logging
import os
from logging.handlers import RotatingFileHandler

# 日志放在用户主目录：onefile 打包后程序目录是 PyInstaller 临时解包区（只读），
# 写日志会失败；用户目录永远可写，且日志跟随用户不随程序位置变化。
LOG_DIR = os.path.join(os.path.expanduser("~"), ".jingjing")
LOG_FILE = os.path.join(LOG_DIR, "jingjing.log")

CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s (%(threadName)s): %(message)s"

_configured = False


def setup_logging(level: int = logging.INFO, log_file: str = LOG_FILE) -> None:
    """
    配置应用日志（幂等，重复调用无副作用）。
    所有业务模块使用 jingjing.* 命名空间的 logger。
    """
    global _configured
    if _configured:
        return

    app_logger = logging.getLogger("jingjing")
    app_logger.setLevel(level)
    app_logger.propagate = False  # 避免第三方库（openai/selenium 等）日志混入

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    app_logger.addHandler(console)

    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)  # 确保日志目录存在
        file_handler = RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        app_logger.addHandler(file_handler)
    except OSError:
        pass  # 日志文件不可写时仅保留控制台输出

    app_logger.info("日志系统初始化完成（文件：%s）", log_file)
    _configured = True
