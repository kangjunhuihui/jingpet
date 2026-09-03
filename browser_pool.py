# browser_pool.py
# 搜索浏览器实例池：跨搜索复用浏览器实例，空闲超时自动关闭。
#
# 为什么需要池：每次搜索都新开浏览器启动慢、占资源；而 ChatWorker 每次
# 消息都会重建（主窗口每轮新开线程），所以浏览器实例由池持有、跨 worker
# 复用。池实例由 MainWindow 创建并注入 ChatWorker（依赖注入，便于测试）。
# 所有方法线程安全（单锁 + 代际号防止竞态）。

import logging
import threading
import time

from config import BROWSER_IDLE_TIMEOUT

logger = logging.getLogger("jingjing.browser_pool")


def _driver_alive(driver) -> bool:
    """
    探测 driver 是否仍然可用：
    - 底层进程已退出（如主人手动关闭了浏览器窗口）→ 失效；
    - 轻量命令失败（current_url 调用报错）→ 失效。
    """
    service = getattr(driver, "service", None)
    process = getattr(service, "process", None) if service is not None else None
    if process is not None and process.poll() is not None:
        return False
    try:
        driver.current_url
        return True
    except Exception:
        return False


class BrowserPool:
    """
    线程安全的浏览器池。

    - acquire(create_fn)：获取可复用的实例（复用 / 空闲超时重建 / 失效重建）；
    - release()：归还实例，并启动空闲计时；
    - close()：立即关闭当前实例（搜索失败或应用退出时调用），幂等。
    """

    def __init__(self, idle_timeout_seconds: float = BROWSER_IDLE_TIMEOUT):
        self._idle_timeout = idle_timeout_seconds
        self._driver = None
        self._lock = threading.Lock()
        self._idle_timer = None
        self._epoch = 0      # 代际号：每次 acquire/close 递增，用于作废过期定时器
        self._last_used = 0.0

    # ---------- 对外接口 ----------

    def acquire(self, create_fn):
        """
        获取可复用的浏览器实例：
        - 已有实例且未超时、仍然存活 → 复用；
        - 空闲超时或实例失效 → 关闭后重建；
        - 否则新建。
        """
        with self._lock:
            self._cancel_idle_timer()
            self._epoch += 1
            now = time.time()

            if self._driver is not None:
                stale = now - self._last_used > self._idle_timeout
                if stale or not _driver_alive(self._driver):
                    reason = "空闲超时" if stale else "实例已失效"
                    logger.info("重建浏览器实例（原因：%s）", reason)
                    self._close_locked()

            if self._driver is None:
                logger.info("新建浏览器实例")
                self._driver = create_fn()
            else:
                logger.debug("复用浏览器实例")

            self._last_used = now
            return self._driver

    def release(self) -> None:
        """归还实例并启动空闲计时；无实例时为空操作。"""
        with self._lock:
            if self._driver is None:
                return
            self._last_used = time.time()
            self._schedule_idle_close()

    def close(self) -> None:
        """立即关闭当前实例（搜索失败 / 应用退出时调用），幂等。"""
        with self._lock:
            self._epoch += 1
            self._cancel_idle_timer()
            self._close_locked()

    # ---------- 内部实现 ----------

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_timer()
        epoch = self._epoch
        timer = threading.Timer(self._idle_timeout, self._on_idle, args=(epoch,))
        timer.daemon = True  # 不阻塞进程退出
        self._idle_timer = timer
        timer.start()

    def _on_idle(self, epoch: int) -> None:
        """空闲超时回调（定时器线程执行）。"""
        with self._lock:
            # 期间若发生过 acquire/close，代际号已变化，本回调作废
            if epoch != self._epoch or self._driver is None:
                return
            logger.info("浏览器空闲超时（%.0f 秒），自动关闭", self._idle_timeout)
            self._close_locked()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _close_locked(self) -> None:
        if self._driver is None:
            return
        try:
            self._driver.quit()
        except Exception:
            logger.exception("关闭浏览器实例时出错")
        finally:
            self._driver = None
