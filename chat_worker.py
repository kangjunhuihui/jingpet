# chat_worker.py
# 后台工作线程：普通聊天、图片识别、必应搜索 + AI 总结
#
# 本模块要点：
# - 指数退避重试（仅限流 / 连接错误，其余异常直接抛出）
# - 草稿历史：流式成功后才并入真实历史，失败直接丢弃
# - stop_flag：支持用户中途停止生成（保留已输出部分）
# - mood_changed 信号：情绪检测在 Worker 内完成，UI 只负责响应
# - 上下文预算：超限时自动截断最早的非系统消息
# - DOM 多重选择器 + 正则兜底解析必应结果
# - 浏览器工厂：edge / chrome / firefox，可选 webdriver-manager

import json
import logging
import os
import re
import threading
import time

from bs4 import BeautifulSoup
from openai import APIConnectionError, RateLimitError
from PySide6.QtCore import QObject, Signal
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app_launcher import close_app, launch_app, normalize
from browser_pool import BrowserPool
from file_creator import create_file
from config import (
    BING_RESULT_SELECTORS, CHAT_PARAMS, CLEAR_HISTORY_COMMAND_PATTERN,
    CLOSE_COMMAND_PATTERN, INTENT_CONTEXT_MESSAGES, INTENT_MAX_TOKENS,
    INTENT_SYSTEM_PROMPT, LOCK_COMMAND_PATTERN, MAX_CONTEXT_TOKENS, MEDIA_NEXT_PATTERN,
    MEDIA_PLAY_PAUSE_PATTERN, MEDIA_PREV_PATTERN, MEDIA_STOP_PATTERN,
    MULTI_CMD_SEPARATORS, OPEN_COMMAND_PATTERN, REBOOT_COMMAND_PATTERN,
    SEARCH_BROWSER, SEARCH_COMMANDS, SEARCH_TIMEOUT, SHUTDOWN_COMMAND_PATTERN,
    SYSTEM_PROMPT, TEXT_MODEL, VISION_MODEL, VOLUME_DOWN_N_PATTERN,
    VOLUME_DOWN_PATTERN, VOLUME_MAX_PATTERN, VOLUME_MUTE_PATTERN, VOLUME_SET_PATTERN, VOLUME_STEP,
    VOLUME_UNMUTE_PATTERN, VOLUME_UP_N_PATTERN, VOLUME_UP_PATTERN,
    WEBSITE_INTENT_WORDS, resource_path,
)
from reminder import DEFAULT_REMINDER_CONTENT, parse_reminder
from system_control import (
    get_volume, lock_screen, media_next_track, media_play_pause,
    media_prev_track, media_stop, reboot_computer, set_volume,
    toggle_mute, volume_down, volume_up,
)
from utils import (
    detect_final_mood, detect_mood_by_keywords, encode_image,
    estimate_tokens, get_image_mime_type,
)

logger = logging.getLogger("jingjing.chat_worker")

# 各分支的 token 上限
NORMAL_MAX_TOKENS = 2048
SEARCH_MAX_TOKENS = 1024  # 极简总结 + 链接，1024 足够
# 视觉模型是推理型模型，会先消耗大量 reasoning token 再输出；
# 预算太小会被思考吃光导致零输出（表现为"卡住"），故给足空间
VISION_MAX_TOKENS = 4096

# 模型思考完仍无内容时的兜底回复（用户至少能看到反馈，而不是无限"思考中"）
EMPTY_REPLY_FALLBACK = "呜～主人，鲸鲸刚才走神了，什么也没说出来…… 再问人家一次好不好？"

# 实时情绪检测间隔（每输出 N 个字符检测一次）
MOOD_CHECK_INTERVAL = 12
# 流式情绪检测滑窗：只对最近 N 字符计分，避免早期情绪累积压制后期内容
# （足够大，长回复中持续的情绪不会被自己滚出窗口）
MOOD_WINDOW = 400
# 情绪最短驻留：切到新情绪后至少保持 N 秒才允许再切（唯一防抖手段；最终情绪不受限）
MOOD_MIN_HOLD_SECONDS = 2.0

# API 重试策略（指数退避，仅限流 / 连接错误）
RETRY_ATTEMPTS = 3
RETRY_MIN_WAIT = 2.0
RETRY_MAX_WAIT = 10.0
RETRYABLE_EXCEPTIONS = (RateLimitError, APIConnectionError)

SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)
SEARCH_USER_AGENT_FIREFOX = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
)

TIMEOUT_ERROR_MSG = (
    "呜～主人，必应它半天没理人家，搜索结果一直没出来…… "
    "可能是网有点卡，要不你亲自看看浏览器窗口，鲸鲸在旁边给你加油～ 🥺"
)

PARSE_ERROR_MSG = "鲸鲸看不懂这个页面，主人帮我看看浏览器吧～"

NO_BROWSER_ERROR_MSG = "呜～主人，鲸鲸在这台电脑上找不到 Edge 浏览器，装一个再让鲸鲸搜索好不好～"

# "打开"命令缺应用名时的提示
OPEN_NEED_NAME_MSG = "主人，想让我打开什么呀？比如：打开bilibili～"
# "关闭"命令缺应用名时的提示
CLOSE_NEED_NAME_MSG = "主人，想让我关掉什么呀？比如：关闭bilibili～"

# 打开网址（默认浏览器）
OPEN_URL_SUCCESS_MSG = "主人，已经用浏览器打开 {url} 啦～"
OPEN_URL_FAILED_MSG = "呜～主人，鲸鲸没能打开浏览器，你自己开一下吧～"
# 网站意图（含"官网/网站"等字样）但模型也给不出官网地址时的提示
WEBSITE_URL_FAILED_MSG = "呜～主人，鲸鲸不知道「{name}」的官网地址，直接把网址发给鲸鲸好不好？"

# 裸域名判定（"打开bilibili.com"）：至少一个点 + 顶级域 ≥2 个字母，可带端口/路径。
# 单段词（steam、splayer、python官网 不带点）永远不命中 → 不会抢走本地应用查找。
BARE_URL_RE = re.compile(r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s]*)?$")
# 模型生成的 URL 校验：只认 http/https 完整地址（防模型输出 javascript: 等危险协议）
LLM_URL_RE = re.compile(r"^(https?://[^\s<>\"'，。、；！？（）【】()]+)$", re.IGNORECASE)
# 从带杂质的文本中提取第一个 http(s):// 地址（模型可能输出"官网是https://xxx"）；
# 排除中英文标点（含英文括号），避免 URL 吞进后续文字
LLM_URL_EXTRACT_RE = re.compile(
    r"https?://[^\s<>\"'，。、；！？（）【】()]+", re.IGNORECASE)
# 提取后清理 URL 尾部可能残留的标点
_URL_TRAILING_PUNCT = ".,;:!?)]}，。、；：！？）】"


def _is_url(text: str) -> bool:
    """判断是否为明确网址：显式 http(s):// 开头，或符合裸域名结构（如 bilibili.com）。"""
    t = text.strip()
    if not t:
        return False
    if re.match(r"^https?://", t, re.I):
        return True
    return bool(BARE_URL_RE.match(t))


def _contains_multi_separator(text: str) -> bool:
    """文本是否含多命令连接词（然后/接着/再/并且/同时/顺便/，/,）。
    用于"打开应用"分支防吞并：提取出的应用名若含连接词，说明后半段是
    未识别的命令，不应整句当作应用名，交还模型意图理解。"""
    return any(sep in text for sep in MULTI_CMD_SEPARATORS)


def _is_website_intent(name: str) -> bool:
    """
    网站意图判定：名字含「官网/网站/网址/主页/首页/网页/站点」等字样时，
    用户要的是网页而非本地应用（"打开steam官网"不该误开 Steam 客户端）。
    """
    return any(word in name for word in WEBSITE_INTENT_WORDS)


def _parse_llm_url(raw: str) -> str | None:
    """
    解析模型输出的网址：先试整段纯 URL，失败则从文本中提取第一个 http(s):// 地址
    （模型常会带杂质，如"Steam官网是https://store.steampowered.com/"），
    提取后清理尾部残留标点。只认 http/https；输出"无"/无 URL/危险协议 → None。
    """
    text = (raw or "").strip().strip("\"'“”‘’")
    match = LLM_URL_RE.match(text)
    if match:
        return match.group(1).rstrip(_URL_TRAILING_PUNCT) or None
    match = LLM_URL_EXTRACT_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(_URL_TRAILING_PUNCT) or None

# 关机：回复后延迟 SHUTDOWN_REPLY_DELAY 秒执行（让打字机把"晚安"播完再关）
SHUTDOWN_REPLY_MSG = "主人，鲸鲸这就去关机啦～ 晚安！"
SHUTDOWN_REPLY_DELAY = 2.0
SHUTDOWN_CMD = ["shutdown", "/s", "/t", "0"]  # 立即关机（Windows 自带，列表参数无注入）

# 锁屏 / 重启
LOCK_SUCCESS_MSG = "主人，鲸鲸帮你锁屏啦～ 回来记得找我哦！"
LOCK_FAILED_MSG = "呜～锁屏失败了，主人手动按 Win+L 吧～"
REBOOT_REPLY_MSG = "主人，鲸鲸这就去重启啦～ 马上回来！"
REBOOT_REPLY_DELAY = 2.0

# 清空历史
CLEAR_HISTORY_MSG = "主人，聊天历史已经清空啦～ 我们重新开始吧！"

# 定时提醒
REMINDER_SCHEDULED_MSG = "主人，鲸鲸记下啦～ {minutes} 分钟后提醒你：{content}"

# 音量控制
VOLUME_FAILED_MSG = "呜～音量调整失败了，主人手动调一下吧～"

# 媒体控制（模拟多媒体键）
MEDIA_TOGGLE_MSG = "主人，鲸鲸帮你按了播放/暂停～"
MEDIA_NEXT_MSG = "主人，已经切到下一首啦～"
MEDIA_PREV_MSG = "主人，回到上一首啦～"
MEDIA_STOP_MSG = "主人，已经停止播放啦～"
MEDIA_FAILED_MSG = "呜～主人，媒体键按不动，你手动按一下键盘上的媒体键吧～"

# 语义兜底：本地找不到应用时，让模型从候选列表里挑最匹配的名字（如 bilibili → 哔哩哔哩）
LLM_MATCH_MAX_TOKENS = 50  # 只需要输出一个名字，给 50 足够
LLM_MATCH_SYSTEM_PROMPT = (
    "你是 Windows 应用查找助手。下面会给你一台电脑上已安装应用的名称列表，"
    "以及用户想打开的应用名。请从列表中选出与用户意图最匹配的一个名称，"
    "只输出该名称本身，不要输出序号、引号或任何解释。"
    "如果列表中没有匹配项，只输出：无"
)

# 网址兜底：本地找不到应用时，让模型从知识库生成官网 URL（如 "python官网" → https://www.python.org/）
LLM_URL_MAX_TOKENS = 100  # 只需要一个 URL
LLM_URL_SYSTEM_PROMPT = (
    "你是桌面助手“鲸鲸”的网址识别器。用户想打开某个网站或官网，"
    "你必须给出该网站的官方完整 URL（以 http:// 或 https:// 开头）。\n"
    "示例：\n"
    "用户想打开：python官网 → https://www.python.org/\n"
    "用户想打开：百度 → https://www.baidu.com/\n"
    "用户想打开：steam官网 → https://store.steampowered.com/\n"
    "用户想打开：splayer → 无\n"
    "规则：只输出 URL 或“无”，不要输出任何解释、引号或多余文字；"
    "只有当用户明确要打开的是本地软件（如 splayer、微信）时，才输出“无”。"
)

# 正则兜底：从页面源码直接提取外链与锚文本
LINK_RE = re.compile(
    r'<a[^>]+href="(https?://[^"\']+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")


def _optional_driver_service(browser: str):
    """
    优先使用 webdriver-manager 自动管理驱动版本；
    未安装该库时返回 None，交由 Selenium Manager（selenium ≥ 4.6）兜底。
    """
    try:
        if browser == "edge":
            from selenium.webdriver.edge.service import Service
            from webdriver_manager.microsoft import EdgeChromiumDriverManager
            return Service(EdgeChromiumDriverManager().install())
        if browser == "chrome":
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            return Service(ChromeDriverManager().install())
        if browser == "firefox":
            from selenium.webdriver.firefox.service import Service
            from webdriver_manager.firefox import GeckoDriverManager
            return Service(GeckoDriverManager().install())
    except ImportError:
        logger.info("未安装 webdriver-manager，交由 Selenium Manager 自动管理驱动（%s）", browser)
    return None


class SearchError(Exception):
    """搜索过程中的可恢复错误（携带面向用户的提示文案）"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ChatWorker(QObject):
    chunk_received = Signal(str)
    mood_changed = Signal(str)
    # 参数：完整回复文案 + 本条完成后的剩余命令数（批量收尾判定；随信号定格，
    # 避免主线程跨线程读 batch_remaining 产生竞态导致提前回收线程 → worker 被删）
    finished = Signal(str, int)
    error_occurred = Signal(str)

    def __init__(self, history, client, browser_pool: BrowserPool, reminder_manager=None):
        super().__init__()
        self.history = history
        self.client = client
        self._browser_pool = browser_pool
        self._reminder_manager = reminder_manager  # 定时提醒（由主窗口注入，可空）

        # 停止生成标志（由主窗口控制）
        self.stop_flag = False

        # 流式过程中的情绪检测状态
        self.accumulated_reply = ""
        self._mood_char_count = 0
        # 分层状态机：
        # - 过程态：流式检测到非默认情绪就切（滑窗 + 最短驻留防抖），保证"动感"；
        # - 结果态：回复结束以 AI 标签为准（force 切换，不受驻留限制），保证"准确"；
        # - 无任何依据时保持当前展示，不硬切。
        self._displayed_mood = None   # 当前展示给 UI 的情绪
        self._last_mood_switch = None # 上次切换时间戳（最短驻留）

        # 批量命令剩余数（主窗口据此决定收尾时机；0 = 本轮最后一条）
        self.batch_remaining = 0

    # ---------- 通用：流式请求（含重试与停止支持） ----------
    def _stream_reply(self, messages, model: str, max_tokens: int, wait_fn=None) -> str:
        """发起流式请求，逐块发射 chunk_received，返回完整回复文本。"""
        if self.stop_flag:
            logger.info("收到停止指令，跳过本次请求（model=%s）", model)
            return ""

        # 过程态起点：开始生成 → 立绘切"思考"（有依据，且带动感）
        if self._displayed_mood is None:
            self._emit_mood("思考")

        start = time.perf_counter()
        logger.debug("API 请求开始（model=%s, max_tokens=%d）", model, max_tokens)
        stream = self._call_with_retry(
            lambda: self.client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                max_tokens=max_tokens,
                **CHAT_PARAMS,
            ),
            wait_fn=wait_fn,
        )

        full_reply = ""
        for chunk in stream:
            if self.stop_flag:
                logger.info("收到停止指令，中断流式输出（已保留 %d 字符）", len(full_reply))
                break
            content = chunk.choices[0].delta.content
            if content:
                full_reply += content
                self.chunk_received.emit(content)
                self._accumulate_for_mood(content)

        # 模型思考完却没有任何输出（如推理 token 吃光预算）→ 兜底回复
        if not full_reply and not self.stop_flag:
            full_reply = EMPTY_REPLY_FALLBACK
            self.chunk_received.emit(full_reply)

        self._emit_final_mood(full_reply)
        elapsed = time.perf_counter() - start
        logger.info(
            "API 请求完成（model=%s, 耗时 %.2fs, %d 字符）",
            model, elapsed, len(full_reply),
        )
        return full_reply

    def _call_with_retry(self, fn, attempts: int = RETRY_ATTEMPTS, wait_fn=None):
        """
        对限流 / 连接错误做指数退避重试，其他异常直接抛出。
        wait_fn(attempt) 返回重试前等待秒数（测试可注入 0 等待）。
        """
        last_exc = None
        for attempt in range(attempts):
            try:
                return fn()
            except RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt == attempts - 1:
                    break
                delay = wait_fn(attempt) if wait_fn else self._retry_delay(attempt)
                logger.warning(
                    "API 请求失败（%s），%.1fs 后第 %d 次重试",
                    type(e).__name__, delay, attempt + 1,
                )
                time.sleep(delay)
        raise last_exc

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        """指数退避：2s / 2s / 4s（min=2, max=10）"""
        return min(max(2 ** attempt, RETRY_MIN_WAIT), RETRY_MAX_WAIT)

    # ---------- 情绪检测（流式过程中完成） ----------
    def _accumulate_for_mood(self, content: str):
        self.accumulated_reply += content
        self._mood_char_count += len(content)
        if self._mood_char_count >= MOOD_CHECK_INTERVAL:
            self._mood_char_count = 0
            # 滑窗：只对最近 N 字符计分，情绪跟随最新内容而非被早期文本压制
            candidate = detect_mood_by_keywords(self.accumulated_reply[-MOOD_WINDOW:])
            # 非默认的新情绪 → 立即发射；最短驻留是唯一防抖手段，无需二次确认。
            # "默认"不发射：无依据时保持当前展示，避免立绘乱跳。
            if candidate != "默认":
                self._emit_mood(candidate)

    def _emit_mood(self, mood: str, force: bool = False):
        """发射情绪切换（过程态）。force=True 时跳过最短驻留（用于结果态收尾）。
        同一回复内同一情绪可重复出现（内容说了两次开心就切两次开心），不做凑数替换。"""
        if mood is None or mood == self._displayed_mood:
            return
        if not force and self._last_mood_switch is not None:
            if time.monotonic() - self._last_mood_switch < MOOD_MIN_HOLD_SECONDS:
                return  # 最短驻留：刚切过，本次候选忽略
        self._displayed_mood = mood
        self._last_mood_switch = time.monotonic()
        self.mood_changed.emit(mood)

    def _emit_final_mood(self, full_reply: str):
        """结果态：流式结束后发射最终情绪（优先 AI 标签，回退关键词）。
        最终切换 force 生效（不受驻留限制）；无任何依据（默认）时保持当前，不硬切。"""
        final = detect_final_mood(full_reply)
        if final != "默认":
            self._emit_mood(final, force=True)

    # ---------- 上下文预算 ----------
    def _trim_history(self, max_tokens: int = MAX_CONTEXT_TOKENS) -> int:
        """
        就地截断历史：保留系统提示词与最近的消息，超预算的最早消息被剔除。
        返回移除条数。
        """
        if len(self.history) <= 1:
            return 0
        budget = max_tokens
        kept = []
        removed = 0
        for msg in reversed(self.history[1:]):
            cost = estimate_tokens(msg["content"]) + 4  # 每条消息固定开销
            if budget - cost < 0:
                removed += 1
                continue
            kept.append(msg)
            budget -= cost
        if removed:
            self.history[:] = [self.history[0]] + list(reversed(kept))
            logger.info("上下文超限，截断 %d 条历史消息", removed)
        return removed

    # ---------- 搜索：关键词提取 ----------
    def _extract_search_keyword(self, user_input: str) -> str:
        """去掉触发词，返回真正的搜索关键词（无触发词时返回空串）。"""
        for cmd in SEARCH_COMMANDS:
            if cmd in user_input:
                return user_input.replace(cmd, "").strip()
        return ""

    # ---------- 搜索：浏览器工厂（edge / chrome / firefox） ----------
    def _create_search_driver(self):
        """按配置选择浏览器；未知值回退 Edge。"""
        if SEARCH_BROWSER == "chrome":
            return self._create_chrome_driver()
        if SEARCH_BROWSER == "firefox":
            return self._create_firefox_driver()
        if SEARCH_BROWSER != "edge":
            logger.warning("未知的 SEARCH_BROWSER=%r，回退使用 Edge", SEARCH_BROWSER)
        return self._create_edge_driver()

    def _create_edge_driver(self):
        options = Options()
        options.add_argument("--start-maximized")
        # 防检测参数（降低被必应识别为机器人的概率）
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        # 伪装成真实 Edge
        options.add_argument(f"user-agent={SEARCH_USER_AGENT}")

        # 显式指定 Edge 本体路径（新版 msedgedriver 可能不会自动发现浏览器）
        edge_binary = self._find_edge_binary()
        if edge_binary:
            options.binary_location = edge_binary

        # 优先使用打包内置的 msedgedriver（随 exe 分发，离线可用）
        bundled = resource_path(os.path.join("Assets", "msedgedriver.exe"))
        if os.path.exists(bundled):
            from selenium.webdriver.edge.service import Service
            service = Service(bundled)
            try:
                logger.info("使用内置 msedgedriver：%s", bundled)
                return webdriver.Edge(options=options, service=service)
            except Exception as e:
                # 版本与本地 Edge 不匹配等 → 关闭残留服务，回退自动管理
                logger.warning("内置 msedgedriver 启动失败（%s），回退自动管理驱动", e)
                try:
                    service.stop()
                except Exception:
                    pass

        # 浏览器本体都不存在时，给个明白话而不是裸异常
        if not edge_binary:
            raise SearchError(NO_BROWSER_ERROR_MSG)

        return webdriver.Edge(options=options, service=_optional_driver_service("edge"))

    @staticmethod
    def _find_edge_binary() -> str | None:
        """在常见安装位置查找 Edge 浏览器本体（找不到返回 None）。"""
        candidates = [
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def _create_chrome_driver(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(f"user-agent={SEARCH_USER_AGENT}")
        return webdriver.Chrome(options=options, service=_optional_driver_service("chrome"))

    def _create_firefox_driver(self):
        options = webdriver.FirefoxOptions()
        options.add_argument("--start-maximized")
        options.set_preference("general.useragent.override", SEARCH_USER_AGENT_FIREFOX)
        return webdriver.Firefox(options=options, service=_optional_driver_service("firefox"))

    # ---------- 搜索：抓取与解析（多重降级） ----------
    def _fetch_bing_results(self, keyword: str) -> str:
        """
        使用（或复用）浏览器实例搜索必应并解析前 5 条结果。

        解析降级链：多重 CSS 选择器 → 正则提取链接 → 友好报错。
        失败时关闭浏览器实例；成功时归还池复用。
        """
        driver = self._browser_pool.acquire(self._create_search_driver)
        try:
            driver.get("https://cn.bing.com")

            # 等待搜索框出现并输入关键词（必应搜索框 name="q"）
            try:
                search_box = WebDriverWait(driver, SEARCH_TIMEOUT).until(
                    EC.presence_of_element_located((By.NAME, "q"))
                )
            except TimeoutException:
                raise SearchError(TIMEOUT_ERROR_MSG)
            search_box.send_keys(keyword)
            search_box.send_keys(Keys.RETURN)

            # 等待结果容器（任一已知选择器命中即可）
            try:
                WebDriverWait(driver, SEARCH_TIMEOUT).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ", ".join(BING_RESULT_SELECTORS))
                    )
                )
            except TimeoutException:
                # 页面可能用了未知结构：先尝试正则兜底
                fallback = self._parse_links_regex(driver.page_source)
                if fallback:
                    logger.warning("必应结果选择器等待超时，使用正则兜底（关键词=%s）", keyword)
                    return fallback
                raise SearchError(TIMEOUT_ERROR_MSG)

            results = self._parse_results(driver.page_source)
            if not results:
                raise SearchError(PARSE_ERROR_MSG)

            logger.info("必应搜索成功（关键词=%s, %d 条）", keyword, results.count("标题："))
            return results

        except BaseException:
            # 失败即关闭实例，避免复用状态可疑的浏览器
            self._browser_pool.close()
            raise
        finally:
            self._browser_pool.release()

    def _parse_results(self, page_source: str) -> str:
        """按多重选择器解析结果；全部失败时回退正则提取链接。"""
        soup = BeautifulSoup(page_source, "lxml")
        for selector in BING_RESULT_SELECTORS:
            items = soup.select(selector)[:5]
            if items:
                formatted = self._format_result_items(items)
                if formatted:
                    return formatted
        return self._parse_links_regex(page_source)

    @staticmethod
    def _format_result_items(items) -> str:
        """把解析出的结果条目格式化为文本；条目全部为空时返回空串（触发降级）。"""
        formatted = []
        for idx, item in enumerate(items, start=1):
            title_tag = item.find("h2")
            title = title_tag.get_text(strip=True) if title_tag else ""

            link_tag = item.find("a")
            link = link_tag.get("href") if link_tag and link_tag.get("href") else ""

            summary_tag = item.find("p")
            summary = summary_tag.get_text(strip=True) if summary_tag else ""

            if not title and not link and not summary:
                return ""  # 该选择器命中但无有效内容 → 尝试下一个

            formatted.append(
                f"{idx}. 标题：{title or '无标题'}\n"
                f"   摘要：{summary or '（无摘要）'}\n"
                f"   链接：{link or '#'}\n"
            )
        return "\n".join(formatted)

    def _parse_links_regex(self, page_source: str) -> str:
        """正则兜底：从页面源码直接提取可见链接与锚文本（最多 5 条）。"""
        formatted = []
        count = 0
        for url, raw_title in LINK_RE.findall(page_source):
            title = TAG_RE.sub("", raw_title)
            title = re.sub(r"\s+", " ", title).strip()
            if not title or not url:
                continue
            count += 1
            formatted.append(
                f"{count}. 标题：{title}\n   摘要：（无摘要）\n   链接：{url}\n"
            )
            if count >= 5:
                break
        return "\n".join(formatted)

    def _build_search_messages(self, keyword: str, formatted_results: str) -> list:
        """构造搜索总结的 API 消息（临时消息，不写入历史）。"""
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"这是必应搜索「{keyword}」前5条结果的摘要信息。\n"
                    f"请你以鲸鲸的口吻，用极简的方式概括这些结果主要围绕什么主题"
                    f"（一句话或两句话概括即可，不要展开每条内容）。\n"
                    f"最后**必须**在回复末尾附上这5个链接，格式严格如下：\n"
                    f"主人，链接我都列在下面了～\n"
                    f"1. 标题：xxx\n   链接：xxx\n"
                    f"2. 标题：xxx\n   链接：xxx\n"
                    f"（以此类推，共5条）\n\n"
                    f"以下是搜索结果摘要：\n{formatted_results}"
                ),
            },
        ]

    # ---------- 主入口：文本 ----------
    def send_text(self, user_input: str):
        """
        三层分派：
        1) 多命令拆分（全段可识别才拆）→ 顺序执行；
        2) 快通道单命令（正则，零延迟）；
        3) 模型意图理解兜底（口语/混杂/隐含意图 → JSON 命令；无命令 → 聊天）。
        """
        try:
            self._trim_history()

            # 多命令拆分（如"帮我把音量调到60，然后打开splayer"）
            commands = split_multi_commands(user_input)
            if commands:
                self._dispatch_sequence(commands)
                return

            # 快通道单命令
            if _classify_command(user_input) is not None:
                # 未命中（如"打开xxx然后<口语命令>"：提取的应用名含连接词）→ 交还模型意图理解
                if self._dispatch_single(user_input):
                    return
                actions = self._ask_intent(user_input)
                if actions:
                    self._execute_actions(user_input, actions)
                else:
                    self._send_normal_chat(user_input)
                return

            # 模型意图理解兜底
            actions = self._ask_intent(user_input)
            if actions:
                self._execute_actions(user_input, actions)
            else:
                self._send_normal_chat(user_input)
        except Exception as e:
            logger.exception("发送文本失败（输入=%r）", user_input[:50])
            self.error_occurred.emit(str(e))
            # 仅回滚已入队的用户消息（搜索分支）；普通聊天走草稿，无需回滚
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()

    # ---------- 命令分派（快通道） ----------
    def _dispatch_sequence(self, commands: list):
        """顺序执行多条命令；batch_remaining 在每条 emit 前递减（主窗口据此收尾）。"""
        self.batch_remaining = len(commands)
        for cmd in commands:
            self.batch_remaining -= 1
            self._dispatch_single(cmd)

    def _dispatch_single(self, text: str) -> bool:
        """单条命令分派（快通道正则）：命中返回 True。"""
        if any(cmd in text for cmd in SEARCH_COMMANDS):
            self._send_search(text)  # 显式触发词：快通道，直接搜
            return True
        if re.match(SHUTDOWN_COMMAND_PATTERN, text.strip()):
            # 关机分支必须在"关闭应用"之前（"关闭电脑"会被当成关闭应用"电脑"）
            self._send_shutdown(text)
            return True
        if re.match(LOCK_COMMAND_PATTERN, text.strip()):
            self._send_lock(text)
            return True
        if re.match(REBOOT_COMMAND_PATTERN, text.strip()):
            self._send_reboot(text)
            return True
        if re.match(CLEAR_HISTORY_COMMAND_PATTERN, text.strip()):
            self._send_clear_history(text)
            return True
        if parse_reminder(text) is not None:
            self._send_reminder(text)
            return True
        if self._is_volume_command(text):
            self._send_volume(text)
            return True
        media_action = _classify_media_command(text)
        if media_action is not None:
            self._send_media(text, media_action)
            return True
        close_name = self._extract_app_name(text, CLOSE_COMMAND_PATTERN)
        if close_name:
            if _contains_multi_separator(close_name):
                return False  # 名字含连接词：后半段是未识别命令，交还模型意图理解，不当应用名吞并
            self._send_close_app(text)
            return True
        open_name = self._extract_app_name(text)
        if open_name:
            if _contains_multi_separator(open_name):
                return False  # 同上："打开xxx然后把音量拉满" 不该把整句当应用名
            # "打开浏览器搜索xxx" 已被搜索触发词拦截（搜索优先），
            # 到这里说明是纯"打开应用"命令
            self._send_open_app(text)
            return True
        return False

    # ---------- 模型意图理解兜底 ----------
    def _ask_intent(self, user_input: str) -> list | None:
        """
        让模型理解口语意图 → 结构化命令列表。
        返回 [] 表示无命令（走聊天）；None 表示理解失败（回退聊天）。
        携带最近 INTENT_CONTEXT_MESSAGES 条 user/assistant 历史，
        支持"那换成open.txt"这类指代上文的请求（位置/内容从上下文补全）。
        """
        try:
            messages = [{"role": "system", "content": INTENT_SYSTEM_PROMPT}]
            # 上下文只取最近 N 条（系统提示词不携带，避免噪音与 token 浪费）
            for msg in self.history[-INTENT_CONTEXT_MESSAGES:]:
                if msg.get("role") in ("user", "assistant"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": user_input})
            response = self.client.chat.completions.create(
                model=TEXT_MODEL,
                messages=messages,
                max_tokens=INTENT_MAX_TOKENS,
                temperature=0,  # 命令提取要确定性
                stream=False,
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:
            logger.info("意图理解失败，回退普通聊天：%s", e)
            return None
        return _parse_intent_json(raw)

    def _execute_actions(self, user_input: str, actions: list):
        """执行模型意图命令列表（串行）；search 排最后（流式输出）。"""
        self.history.append({"role": "user", "content": user_input})
        ordered = [a for a in actions if a.get("action") != "search"] \
            + [a for a in actions if a.get("action") == "search"]
        self.batch_remaining = len(ordered)
        for action in ordered:
            self.batch_remaining -= 1
            if action.get("action") == "search":
                self._send_search(
                    user_input,
                    keyword=str(action.get("keyword") or user_input),
                    fallback_to_chat=True,
                    record_user=False,
                )
                continue
            message = self._execute_action(action)
            self.history.append({"role": "assistant", "content": message})
            self.chunk_received.emit(message)
            self.finished.emit(message, self.batch_remaining)

    def _execute_action(self, action: dict) -> str:
        """执行单条结构化命令，返回回复文案。"""
        action_name = action.get("action")
        if action_name == "open_app":
            # 防呆：LLM 可能把域名/官网名塞进 open_app，统一走 _resolve_open
            return self._resolve_open(str(action.get("name") or ""))
        if action_name == "open_url":
            return self._resolve_open(str(action.get("name") or ""))
        if action_name == "close_app":
            _, message = close_app(str(action.get("name") or ""), matcher=self._llm_match_app)
            return message
        if action_name == "volume_set":
            value = max(0, min(100, int(action.get("value") or 50)))
            return f"主人，音量已经调到 {value}% 啦～" if set_volume(value) else VOLUME_FAILED_MSG
        if action_name == "volume_up":
            result = volume_up(int(action.get("step") or VOLUME_STEP))
            return f"主人，音量已经调到 {result}% 啦～" if result is not None else VOLUME_FAILED_MSG
        if action_name == "volume_down":
            result = volume_down(int(action.get("step") or VOLUME_STEP))
            return f"主人，音量已经调到 {result}% 啦～" if result is not None else VOLUME_FAILED_MSG
        if action_name == "mute":
            current = get_volume()
            if current is None:
                return VOLUME_FAILED_MSG
            if current == 0:
                return "主人，已经静音啦～"
            return "主人，已经静音啦～" if toggle_mute() == 0 else VOLUME_FAILED_MSG
        if action_name == "unmute":
            current = get_volume()
            if current is None:
                return VOLUME_FAILED_MSG
            if current > 0:
                return f"主人，音量已经恢复啦～（{current}%）"
            result = toggle_mute()
            return f"主人，音量已经恢复啦～（{result}%）" if result else VOLUME_FAILED_MSG
        if action_name == "play_pause":
            return MEDIA_TOGGLE_MSG if media_play_pause() else MEDIA_FAILED_MSG
        if action_name == "next_track":
            return MEDIA_NEXT_MSG if media_next_track() else MEDIA_FAILED_MSG
        if action_name == "prev_track":
            return MEDIA_PREV_MSG if media_prev_track() else MEDIA_FAILED_MSG
        if action_name == "stop_media":
            return MEDIA_STOP_MSG if media_stop() else MEDIA_FAILED_MSG
        if action_name == "shutdown":
            self._schedule_shutdown()
            return SHUTDOWN_REPLY_MSG
        if action_name == "reboot":
            self._schedule_reboot()
            return REBOOT_REPLY_MSG
        if action_name == "lock":
            return LOCK_SUCCESS_MSG if lock_screen() else LOCK_FAILED_MSG
        if action_name == "clear_history":
            self.history[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
            return CLEAR_HISTORY_MSG
        if action_name == "remind":
            minutes = max(0, int(action.get("minutes") or 0))
            content = str(action.get("content") or "").strip()
            if self._reminder_manager is None:
                return "主人，提醒功能现在不可用哦～"
            self._reminder_manager.schedule(minutes, content or DEFAULT_REMINDER_CONTENT)
            return REMINDER_SCHEDULED_MSG.format(
                minutes=minutes, content=content or DEFAULT_REMINDER_CONTENT)
        if action_name == "create_file":
            # 执行层全权校验（盘符/已存在/父目录/逃逸堵漏），这里只透传模型参数
            _, message = create_file(
                str(action.get("path") or ""),
                str(action.get("content") or ""),
            )
            return message
        return "呜～这个操作鲸鲸还不会呢～"

    # ---------- 关闭本地应用 ----------
    def _extract_app_name(self, user_input: str, pattern: str = OPEN_COMMAND_PATTERN) -> str:
        """从"打开/关闭xxx"类命令中提取应用名；非命令返回空串。"""
        match = re.match(pattern, user_input.strip())
        if not match:
            return ""
        return match.group(1).strip()

    def _finish_tool_reply(self, user_input: str, message: str):
        """工具命令（开/关应用、系统控制、音量、提醒等）的统一收尾：写历史 + 走流式通道。"""
        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": message})
        self.chunk_received.emit(message)
        self.finished.emit(message, self.batch_remaining)

    def _send_close_app(self, user_input: str):
        """关闭本地应用：找到运行中的进程即强杀；没运行/找不到给对应提示。"""
        app_name = self._extract_app_name(user_input, CLOSE_COMMAND_PATTERN)
        if not app_name:
            self._emit_error_reply(CLOSE_NEED_NAME_MSG)
            return
        ok, message = close_app(app_name, matcher=self._llm_match_app)
        self._finish_tool_reply(user_input, message)

    def _send_shutdown(self, user_input: str):
        """关机命令：先回复（写历史 + 走流式通道），延迟片刻后执行关机。"""
        self._finish_tool_reply(user_input, SHUTDOWN_REPLY_MSG)
        self._schedule_shutdown()

    def _schedule_shutdown(self):
        """延迟执行关机（独立定时器线程，不阻塞 worker/主线程）。"""
        timer = threading.Timer(SHUTDOWN_REPLY_DELAY, self._do_shutdown)
        timer.daemon = True
        timer.start()

    @staticmethod
    def _do_shutdown():
        try:
            import subprocess
            subprocess.run(SHUTDOWN_CMD, timeout=10)
            logger.info("已执行关机命令")
        except Exception as e:
            logger.warning("执行关机失败：%s", e)

    def _send_lock(self, user_input: str):
        """锁屏命令：立即锁屏（无需缓冲，锁屏后回来能看到回复）。"""
        ok = lock_screen()
        message = LOCK_SUCCESS_MSG if ok else LOCK_FAILED_MSG
        self._finish_tool_reply(user_input, message)

    def _send_reboot(self, user_input: str):
        """重启命令：先回复，延迟片刻后执行重启（与关机一致）。"""
        self._finish_tool_reply(user_input, REBOOT_REPLY_MSG)
        self._schedule_reboot()

    def _schedule_reboot(self):
        """延迟执行重启（独立定时器线程，不阻塞 worker/主线程）。"""
        timer = threading.Timer(REBOOT_REPLY_DELAY, self._do_reboot)
        timer.daemon = True
        timer.start()

    @staticmethod
    def _do_reboot():
        if not reboot_computer():
            logger.warning("执行重启失败")

    def _send_clear_history(self, user_input: str):
        """清空历史：重置为仅系统提示词（关闭窗口时自动持久化干净历史）。"""
        self.history[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._finish_tool_reply(user_input, CLEAR_HISTORY_MSG)

    def _send_reminder(self, user_input: str):
        """定时提醒：解析时间与内容 → 交给提醒管理器倒计时。"""
        parsed = parse_reminder(user_input)
        if parsed is None:
            return  # send_text 已保证是提醒命令，防御性返回
        minutes, content = parsed
        if self._reminder_manager is None:
            self._emit_error_reply("主人，提醒功能现在不可用哦～")
            return
        self._reminder_manager.schedule(minutes, content)
        message = REMINDER_SCHEDULED_MSG.format(minutes=minutes, content=content)
        self._finish_tool_reply(user_input, message)

    @staticmethod
    def _is_volume_command(text: str) -> bool:
        """是否为音量控制命令（调大/调小/调到/静音/取消静音，含数字版本）。"""
        return _parse_volume_command(text) is not None

    def _send_volume(self, user_input: str):
        """
        音量控制：调到指定值 / 上调/下调指定幅度 / 无数字用默认步长 / 拉满 / 静音切换。
        回复新状态。
        """
        parsed = _parse_volume_command(user_input)
        if parsed is None:
            return  # send_text 已保证是音量命令，防御性返回
        action, value = parsed

        if action == "mute":
            current = get_volume()
            if current is None:
                message = VOLUME_FAILED_MSG
            elif current == 0:
                message = "主人，已经静音啦～"
            else:
                message = "主人，已经静音啦～" if toggle_mute() == 0 else VOLUME_FAILED_MSG
        elif action == "unmute":
            current = get_volume()
            if current is None:
                message = VOLUME_FAILED_MSG
            elif current > 0:
                message = f"主人，音量已经恢复啦～（{current}%）"  # 本来就没静音
            else:
                result = toggle_mute()
                message = (f"主人，音量已经恢复啦～（{result}%）"
                           if result else VOLUME_FAILED_MSG)
        elif action == "max":
            message = ("主人，音量已经拉到最大啦～" if set_volume(100)
                       else VOLUME_FAILED_MSG)
        elif action == "set":
            new_volume = value if set_volume(value) else None
            message = (f"主人，音量已经调到 {new_volume}% 啦～"
                       if new_volume is not None else VOLUME_FAILED_MSG)
        elif action == "up":
            result = volume_up(value if value is not None else VOLUME_STEP)
            message = (f"主人，音量已经调到 {result}% 啦～"
                       if result is not None else VOLUME_FAILED_MSG)
        else:  # down
            result = volume_down(value if value is not None else VOLUME_STEP)
            message = (f"主人，音量已经调到 {result}% 啦～"
                       if result is not None else VOLUME_FAILED_MSG)
        self._finish_tool_reply(user_input, message)

    def _send_media(self, user_input: str, action: str):
        """媒体控制：模拟多媒体键（播放/暂停、下一首、上一首、停止）。"""
        if action == "play_pause":
            message = MEDIA_TOGGLE_MSG if media_play_pause() else MEDIA_FAILED_MSG
        elif action == "next":
            message = MEDIA_NEXT_MSG if media_next_track() else MEDIA_FAILED_MSG
        elif action == "prev":
            message = MEDIA_PREV_MSG if media_prev_track() else MEDIA_FAILED_MSG
        else:  # stop
            message = MEDIA_STOP_MSG if media_stop() else MEDIA_FAILED_MSG
        self._finish_tool_reply(user_input, message)

    def _send_open_app(self, user_input: str):
        """打开应用或网址：明确网址 → 浏览器；否则本地应用查找 → 找不到再让模型生成官网 URL。"""
        app_name = self._extract_app_name(user_input)
        if not app_name:
            self._emit_error_reply(OPEN_NEED_NAME_MSG)
            return
        self._finish_tool_reply(user_input, self._resolve_open(app_name))

    def _resolve_open(self, name: str) -> str:
        """
        打开目标统一解析（快通道与 LLM 意图共用），返回回复文案：
        1) 明确网址（bilibili.com / https://…）→ 默认浏览器直接打开；
        2) 网站意图（含"官网/网站/网址/主页"等字样，如 steam官网）→ 跳过本地查找，
           直接让模型生成官网 URL（防止误开同名本地应用，如 QQ 客户端）；
           模型也给不出 → 友好提示，不回退本地；
        3) 本地应用查找（注册表/开始菜单 + LLM 应用名匹配）→ 启动；
        4) 本地找不到 → LLM 从知识库生成官网 URL（如 哔哩哔哩 → bilibili.com）→ 打开；
        5) 全失败 → 原"没找到"提示。
        """
        if _is_url(name):
            _, message = self._open_url(name)
            return message
        if _is_website_intent(name):
            url = self._llm_url_for(name)
            if url:
                _, message = self._open_url(url)
                return message
            return WEBSITE_URL_FAILED_MSG.format(name=name)
        ok, message = launch_app(name, matcher=self._llm_match_app)
        if ok:
            return message
        url = self._llm_url_for(name)
        if url:
            _, message = self._open_url(url)
            return message
        return message  # 保持"没找到"提示

    @staticmethod
    def _open_url(url: str) -> tuple[bool, str]:
        """用系统默认浏览器打开网址（裸域名自动补 https://）。"""
        import webbrowser

        target = url if re.match(r"^https?://", url, re.I) else f"https://{url}"
        try:
            if webbrowser.open(target):
                return True, OPEN_URL_SUCCESS_MSG.format(url=url)
            return False, OPEN_URL_FAILED_MSG
        except Exception:
            return False, OPEN_URL_FAILED_MSG

    def _ask_url(self, name: str) -> str | None:
        """调用模型生成官网 URL，返回模型原始回复；API 异常返回 None。"""
        try:
            response = self.client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[
                    {"role": "system", "content": LLM_URL_SYSTEM_PROMPT},
                    {"role": "user", "content": f"用户想打开：{name}"},
                ],
                max_tokens=LLM_URL_MAX_TOKENS,
                temperature=0,  # 生成网址要确定性
                stream=False,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.warning("模型生成网址失败（%s），跳过网址兜底", e)
            return None

    def _llm_url_for(self, name: str) -> str | None:
        """
        本地找不到应用时调用：让模型从知识库生成官网 URL。
        最多尝试两次（模型偶发空输出时重试一次）；只认 http(s):// 完整地址；
        模型说"无"/格式非法/API 失败 → None（回退"没找到"提示）。
        纯工具调用，不写入对话历史。
        """
        for attempt in range(2):
            raw = self._ask_url(name)
            if raw is None:
                return None  # API 异常，不重试
            url = _parse_llm_url(raw)
            logger.info("模型生成网址（%s）第 %d 次：%r → %r", name, attempt + 1, raw, url)
            if url is not None:
                return url
        return None

    # ---------- 打开应用：语义兜底（模型从本机候选里挑名字） ----------
    def _llm_match_app(self, user_name: str, candidates: list) -> str | None:
        """
        本地找不到应用时调用：让模型从本机候选应用名里挑最匹配的一个。
        纯工具调用，不写入对话历史；任何失败都返回 None（回退"没找到"提示）。
        """
        if not candidates:
            return None
        try:
            response = self.client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[
                    {"role": "system", "content": LLM_MATCH_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"用户想打开：{user_name}\n应用列表：\n" + "\n".join(candidates),
                    },
                ],
                max_tokens=LLM_MATCH_MAX_TOKENS,
                temperature=0,  # 挑名字要确定性，不用人设参数
                stream=False,
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:
            logger.warning("模型匹配应用失败（%s），回退'没找到'提示", e)
            return None
        return _parse_llm_choice(raw, candidates)

    def _send_normal_chat(self, user_input: str):
        user_msg = {"role": "user", "content": user_input}
        # 草稿历史：先发临时列表，流式成功后才并入真实历史
        messages = self.history + [user_msg]
        full_reply = self._stream_reply(messages, TEXT_MODEL, max_tokens=NORMAL_MAX_TOKENS)

        self.history.append(user_msg)
        if full_reply:
            self.history.append({"role": "assistant", "content": full_reply})
        self.finished.emit(full_reply, self.batch_remaining)

    def _send_search(self, user_input: str, keyword: str | None = None,
                     fallback_to_chat: bool = False, record_user: bool = True):
        """
        执行搜索并让 AI 总结。
        - keyword: 搜索关键词（None 时从触发词提取，无则默认 DeepSeek）；
        - fallback_to_chat: 模型决策触发的搜索失败时静默回退普通聊天
          （用户没显式要求搜）；触发词显式触发的失败仍走错误文案；
        - record_user: 是否记录用户消息（批量执行时已在 _execute_actions 记录）。
        """
        search_keyword = keyword or (self._extract_search_keyword(user_input) or "DeepSeek")

        # 1. 记录用户消息到历史
        if record_user:
            self.history.append({"role": "user", "content": user_input})

        # 2. 使用（或复用）浏览器抓取结果
        try:
            formatted_results = self._fetch_bing_results(search_keyword)
        except SearchError as e:
            logger.warning("必应搜索失败（关键词=%s）：%s", search_keyword, e.message)
            if fallback_to_chat:
                # 模型决策触发的搜索：静默回退普通聊天，不给用户报错
                if record_user:
                    self.history.pop()  # 移除刚入队的用户消息（普通聊天走草稿）
                logger.info("模型决策搜索失败（%s），回退普通聊天", e.message)
                self._send_normal_chat(user_input)
                return
            self._emit_error_reply(e.message)
            return

        # 3. 构造临时消息（不写入历史），调用 AI 做极简总结
        api_messages = self._build_search_messages(search_keyword, formatted_results)
        full_reply = self._stream_reply(api_messages, TEXT_MODEL, max_tokens=SEARCH_MAX_TOKENS)

        # 4. 将 AI 回复写入历史记录
        self.history.append({"role": "assistant", "content": full_reply})
        self.finished.emit(full_reply, self.batch_remaining)
        # 成功路径下浏览器保留在池中复用，空闲超时后自动关闭

    def _emit_error_reply(self, message: str):
        """搜索失败时的统一收尾：提示、写入历史、结束本轮。"""
        self.chunk_received.emit(message)
        self.history.append({"role": "assistant", "content": message})
        self.finished.emit(message, self.batch_remaining)

    # ---------- 主入口：图片 ----------
    def send_image(self, image_path: str, question_text: str):
        try:
            self._trim_history()
            if image_path.startswith(("http://", "https://")):
                image_url = image_path
                logger.debug("图片识别开始（URL 图片）")
            else:
                if not os.path.exists(image_path):
                    logger.warning("图片文件不存在：%s", image_path)
                    self.error_occurred.emit(f"文件不存在：{image_path}")
                    return
                mime = get_image_mime_type(image_path)
                image_url = f"data:{mime};base64,{encode_image(image_path)}"
                logger.debug("图片识别开始（本地图片：%s）", image_path)

            messages = self.history.copy()
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": question_text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            })

            full_reply = self._stream_reply(messages, VISION_MODEL, max_tokens=VISION_MAX_TOKENS)

            self.history.append({"role": "user", "content": question_text})
            if full_reply:
                self.history.append({"role": "assistant", "content": full_reply})
            self.finished.emit(full_reply, self.batch_remaining)

        except Exception as e:
            logger.exception("图片识别失败（图片=%s）", image_path)
            self.error_occurred.emit(str(e))


def _parse_llm_choice(raw: str, candidates: list) -> str | None:
    """
    解析模型输出：容忍引号/序号（"3. 哔哩哔哩"）；输出必须是候选之一才认
    （归一化比较），模型编造的名字直接按失败处理。返回候选原始名或 None。
    """
    text = (raw or "").strip().strip("\"'“”‘’")
    if not text or text == "无":
        return None
    # 去前导序号（"3. xxx" / "3) xxx" / "3、xxx"）
    text = re.sub(r"^\d+[.)、:：]\s*", "", text).strip()
    for cand in candidates:
        if text == cand or normalize(text) == normalize(cand):
            return cand
    return None


def _parse_volume_command(text: str) -> tuple[str, int | None] | None:
    """
    解析音量命令 → (动作, 数值)；非音量命令返回 None。
    动作：set（调到指定值）/ up / down / max（拉满）/ mute / unmute；
    无数字的 up/down 数值为 None（调用方用默认步长 VOLUME_STEP）。
    """
    stripped = text.strip()
    for pattern, action in (
        (VOLUME_SET_PATTERN, "set"),
        (VOLUME_UP_N_PATTERN, "up"),
        (VOLUME_DOWN_N_PATTERN, "down"),
    ):
        match = re.match(pattern, stripped)
        if match:
            return action, int(match.group(1))
    if re.match(VOLUME_MAX_PATTERN, stripped):
        return "max", None
    if re.match(VOLUME_MUTE_PATTERN, stripped):
        return "mute", None
    if re.match(VOLUME_UNMUTE_PATTERN, stripped):
        return "unmute", None
    if re.match(VOLUME_UP_PATTERN, stripped):
        return "up", None
    if re.match(VOLUME_DOWN_PATTERN, stripped):
        return "down", None
    return None


# ---------- 多命令拆分 / 命令分类 ----------

def _classify_media_command(text: str) -> str | None:
    """媒体命令判定：返回 play_pause / next / prev / stop；非媒体命令返回 None。"""
    stripped = text.strip()
    if re.match(MEDIA_PLAY_PAUSE_PATTERN, stripped):
        return "play_pause"
    if re.match(MEDIA_NEXT_PATTERN, stripped):
        return "next"
    if re.match(MEDIA_PREV_PATTERN, stripped):
        return "prev"
    if re.match(MEDIA_STOP_PATTERN, stripped):
        return "stop"
    return None


def _classify_command(text: str) -> str | None:
    """快通道命令识别：返回命令类型名或 None（供多命令拆分判定与单命令分派）。"""
    stripped = text.strip()
    if any(cmd in stripped for cmd in SEARCH_COMMANDS):
        return "search"
    if re.match(SHUTDOWN_COMMAND_PATTERN, stripped):
        return "shutdown"
    if re.match(LOCK_COMMAND_PATTERN, stripped):
        return "lock"
    if re.match(REBOOT_COMMAND_PATTERN, stripped):
        return "reboot"
    if re.match(CLEAR_HISTORY_COMMAND_PATTERN, stripped):
        return "clear_history"
    if parse_reminder(stripped) is not None:
        return "remind"
    if _parse_volume_command(stripped) is not None:
        return "volume"
    if _classify_media_command(stripped) is not None:
        return "media"
    if re.match(CLOSE_COMMAND_PATTERN, stripped):
        return "close_app"
    if re.match(OPEN_COMMAND_PATTERN, stripped):
        return "open_app"
    return None


def split_multi_commands(text: str) -> list | None:
    """
    按连接词切段；切出 ≥2 段且每段都是可识别命令才拆，否则 None。
    例如"帮我把音量调到60，然后打开splayer" → 两段都命中 → 拆分；
    "先吃饭然后睡觉" → 两段都不是命令 → 不拆（正常聊天）。
    """
    pattern = "|".join(re.escape(sep) for sep in MULTI_CMD_SEPARATORS)
    parts = [p.strip() for p in re.split(pattern, text) if p.strip()]
    if len(parts) < 2:
        return None
    if all(_classify_command(p) is not None for p in parts):
        return parts
    return None


# ---------- 模型意图 JSON 解析 ----------

INTENT_ACTIONS = {
    "open_app", "close_app", "open_url", "volume_set", "volume_up",
    "volume_down", "mute", "unmute", "play_pause", "next_track",
    "prev_track", "stop_media", "shutdown", "reboot", "lock",
    "clear_history", "remind", "search", "create_file",
}


def _parse_intent_json(raw: str) -> list | None:
    """
    解析模型输出的命令 JSON：{"commands": [{"action": ..., ...}]}。
    容忍 ```json 围栏与杂质文本；非法 action 丢弃；解析失败返回 None（回退聊天）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return None
    commands = data.get("commands")
    if not isinstance(commands, list):
        return []
    result = []
    for item in commands:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        if action in INTENT_ACTIONS:
            result.append(item)
    return result
