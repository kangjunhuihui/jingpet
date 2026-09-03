# app_launcher.py
# 本地应用查找、启动与关闭（供"打开xxx" / "关闭xxx"命令使用）：
#   1) 注册表 App Paths：可被 Win+R 运行框直接调起的程序（键名 <程序名>.exe，值指向 exe 路径）；
#   2) 开始菜单快捷方式：递归扫描 .lnk，文件名模糊匹配（覆盖中文名，如"哔哩哔哩"）。
# 本地匹配失败时可注入语义 matcher（如 LLM）：从本机候选应用名列表中挑出与用户意图
# 最匹配的名字，再用该名字走本地查找。模型只能从候选列表里挑，路径始终来自本地扫描，
# 无法编造路径，无注入面。仍找不到时返回固定提示文案（不回退网页）。
# 只启动/关闭查找到的真实路径（exe / .lnk），用户输入仅用于匹配，绝不拼接 shell 命令。
# 关闭流程：find_app 拿到路径 → 从 exe 或 .lnk 目标解析出进程名 → taskkill /F 强杀
# （用户拍板：直接强杀，不先温和关闭）。

import logging
import os
import struct
import subprocess

logger = logging.getLogger("jingjing.app_launcher")

# -------- 面向用户的固定文案 --------
OPEN_SUCCESS_TEMPLATE = "主人，已经帮你打开 {name} 啦～"
OPEN_NOT_FOUND_TEMPLATE = "呜～主人，我在电脑上没找到「{name}」呢，装一个再让鲸鲸帮你打开吧～"
CLOSE_SUCCESS_TEMPLATE = "主人，已经帮你关掉 {name} 啦～"
CLOSE_NOT_RUNNING_TEMPLATE = "主人，{name} 现在没有在运行哦～"
CLOSE_FAILED_TEMPLATE = "呜～主人，鲸鲸没能关掉 {name}，你手动关一下试试吧～"

# -------- 注册表 App Paths --------
APP_PATHS_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

# -------- 开始菜单目录（所有用户在前，当前用户在后） --------
_START_MENU_DIRS = (
    os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"),
        "Microsoft", "Windows", "Start Menu", "Programs",
    ),
    os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs",
    ),
)

# 归一化时剥离的常见后缀（"Bilibili客户端" → "bilibili"）
_SUFFIXES_TO_STRIP = ("客户端", "软件", "程序", "桌面版")
# 归一化时移除的空白与常见分隔符
_SEPARATORS = (" ", "-", "_", "·", "—")
# 快捷方式名含这些词时视为"非应用本体"（帮助/卸载等），模糊匹配时跳过，避免误命中
_NOISE_SUBSTRINGS = ("help", "uninstall", "readme", "帮助", "卸载", "自述")

# 语义兜底时给模型的候选应用名数量上限（防御性截断；本机一般 100~300 个）
CANDIDATE_LIMIT = 300


def normalize(name: str) -> str:
    """归一化应用名：小写、去空白与分隔符、去常见后缀（用于匹配比较）。"""
    s = name.strip().lower()
    for ch in _SEPARATORS:
        s = s.replace(ch, "")
    for suffix in _SUFFIXES_TO_STRIP:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s


# ---------- 1) 注册表 App Paths ----------

def _query_app_paths(normalized: str) -> str | None:
    """
    在 App Paths 注册表中按 <名称>.exe 键名精确查找（键名不区分大小写）。
    遍历 HKCU / HKLM × 64/32 位视图，返回 exe 完整路径；未命中返回 None。
    非 Windows 平台（无 winreg）直接返回 None。
    """
    try:
        import winreg
    except ImportError:
        return None
    exe_key = f"{normalized}.exe"
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(root, APP_PATHS_SUBKEY, 0, winreg.KEY_READ | view) as base:
                    try:
                        with winreg.OpenKey(base, exe_key) as key:
                            path, _ = winreg.QueryValueEx(key, None)
                    except FileNotFoundError:
                        continue  # 该视图无此键，换下一个
            except OSError:
                continue
            if path and os.path.isfile(path):
                return path
    return None


def _list_app_paths_names() -> list:
    """
    枚举 App Paths 注册表下所有键名（去掉 .exe 后缀），供语义兜底的候选列表使用。
    遍历 HKCU / HKLM × 64/32 位视图，归一化去重；非 Windows 平台返回空列表。
    """
    try:
        import winreg
    except ImportError:
        return []
    seen, names = set(), []
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(root, APP_PATHS_SUBKEY, 0, winreg.KEY_READ | view) as base:
                    index = 0
                    while True:
                        try:
                            raw = winreg.EnumKey(base, index)
                            index += 1
                        except OSError:
                            break  # 枚举完毕
                        name = os.path.splitext(raw)[0]
                        key = normalize(name)
                        if key and key not in seen:
                            seen.add(key)
                            names.append(name)
            except OSError:
                continue
    return names


# ---------- 2) 开始菜单快捷方式 ----------

_start_menu_cache = None  # 模块级缓存：[(显示名, .lnk 完整路径)]


def _scan_start_menu() -> list:
    """递归扫描开始菜单目录下的所有 .lnk（结果缓存，避免每次命令重复扫描）。"""
    global _start_menu_cache
    if _start_menu_cache is not None:
        return _start_menu_cache
    found = []
    for base in _START_MENU_DIRS:
        if not base or not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".lnk"):
                    found.append((os.path.splitext(f)[0], os.path.join(root, f)))
    _start_menu_cache = found
    logger.info("开始菜单扫描完成，共 %d 个快捷方式", len(found))
    return found


def _match_start_menu(name: str) -> str | None:
    """
    在开始菜单中模糊匹配应用名：精确相等优先，其次双向子串（取最短候选，减少误命中）。
    返回 .lnk 完整路径（直接用 os.startfile 启动，无需解析快捷方式目标）。
    """
    target = normalize(name)
    if not target:
        return None
    exact, fuzzy = [], []
    for display, path in _scan_start_menu():
        cand = normalize(display)
        if not cand:
            continue
        if any(noise in cand for noise in _NOISE_SUBSTRINGS):
            continue  # "7-Zip Help" 这类条目不参与匹配，避免误命中
        if cand == target:
            exact.append(path)
        elif target in cand or cand in target:
            fuzzy.append((len(cand), path))
    if exact:
        return exact[0]
    if fuzzy:
        fuzzy.sort()
        return fuzzy[0][1]
    return None


# ---------- 语义兜底：候选应用名列表 ----------

def get_candidates(limit: int = CANDIDATE_LIMIT) -> list:
    """
    收集本机应用候选名列表（供语义 matcher 挑选）：
    开始菜单快捷方式显示名在前（含中文名），注册表 App Paths 键名补充，归一化去重。
    超过 limit 时截断（防御性上限）。
    """
    seen, out = set(), []
    start_menu_names = [display for display, _path in _scan_start_menu()]
    for raw in start_menu_names + _list_app_paths_names():
        key = normalize(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(raw)
        if len(out) >= limit:
            break
    return out


def _resolve_picked_name(picked: str, candidates: list) -> str | None:
    """把语义 matcher 挑中的名字与候选列表对账：必须是候选之一才认（防止模型编造）。"""
    picked_norm = normalize(picked) if picked else ""
    if not picked_norm:
        return None
    for cand in candidates:
        if picked_norm == normalize(cand):
            return cand
    return None


# ---------- 对外接口 ----------

def find_app(name: str, matcher=None) -> str | None:
    """
    按名称查找应用路径：先注册表 App Paths（英文 exe 名），后开始菜单（中文/模糊）。
    本地匹配失败且提供了 matcher 时，用 matcher 从候选列表中挑名字再走一遍本地查找；
    matcher 形如 matcher(user_name, candidates) -> 候选名或 None。
    """
    normalized = normalize(name)
    if not normalized:
        return None
    path = _query_app_paths(normalized)
    if path:
        logger.info("应用命中注册表 App Paths：%s -> %s", name, path)
        return path
    lnk = _match_start_menu(name)
    if lnk:
        logger.info("应用命中开始菜单快捷方式：%s -> %s", name, lnk)
        return lnk

    # 本地匹配失败 → 可选语义兜底（如 LLM 别名匹配）
    if matcher is not None:
        candidates = get_candidates()
        picked = matcher(name, candidates)
        resolved = _resolve_picked_name(picked, candidates)
        if resolved:
            logger.info("语义兜底命中：%s -> %s", name, resolved)
            path2 = _query_app_paths(normalize(resolved))
            if path2:
                return path2
            lnk2 = _match_start_menu(resolved)
            if lnk2:
                return lnk2
            logger.warning("语义兜底挑中的名字无法定位：%s", resolved)
    logger.info("未找到应用：%s", name)
    return None


def launch_app(name: str, matcher=None) -> tuple[bool, str]:
    """启动应用，返回 (是否成功, 面向用户的文案)。只启动查找到的真实路径。"""
    target = find_app(name, matcher=matcher)
    if not target:
        return False, OPEN_NOT_FOUND_TEMPLATE.format(name=name)
    start = getattr(os, "startfile", None)
    if start is None:
        return False, OPEN_NOT_FOUND_TEMPLATE.format(name=name)
    try:
        start(target)
    except OSError as e:
        logger.warning("启动应用失败（%s）：%s", target, e)
        return False, OPEN_NOT_FOUND_TEMPLATE.format(name=name)
    logger.info("已启动应用：%s（%s）", name, target)
    return True, OPEN_SUCCESS_TEMPLATE.format(name=name)


# ---------- 关闭应用 ----------

def _parse_lnk_target(lnk_path: str) -> str | None:
    """
    解析 .lnk 快捷方式的目标路径（Windows Shell Link 二进制格式，零依赖）。
    1) 规范解析：LinkInfo 的 LocalBasePathOffsetUnicode（UTF-16）/ LocalBasePathOffset（ANSI）；
    2) 全文扫描兜底：真实 .lnk 常把路径以 ANSI 存于 LinkInfo 尾部（Unicode 偏移字段
       实际指向 ANSI 文本），故再按 ANSI/UTF-16 两种编码扫描盘符绝对路径，
       取真实存在且以 .exe 结尾的最后一条（IDList 目标项通常最靠后）。
    全部失败返回 None（调用方回退到"无法关闭"提示）。
    """
    try:
        with open(lnk_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    target = _parse_lnk_local_base_path(data)
    if target:
        return target
    return _scan_lnk_abs_path(data)


def _is_valid_target(path: str) -> bool:
    """候选目标合法性：绝对路径、.exe 结尾、真实存在（过滤二进制乱码误报）。"""
    return bool(path) and os.path.isabs(path) and path.lower().endswith(".exe") \
        and os.path.exists(path)


def _parse_lnk_local_base_path(data: bytes) -> str | None:
    """规范解析：按 Shell Link 结构读 LinkInfo 的 LocalBasePath 字段。"""
    if len(data) < 0x4C:
        return None
    flags = struct.unpack_from("<I", data, 0x14)[0]
    offset = 0x4C
    if flags & 0x1:  # HasLinkTargetIDList：跳过 IDList
        if offset + 2 > len(data):
            return None
        size = struct.unpack_from("<H", data, offset)[0]
        offset += 2 + size
    if not (flags & 0x2) or offset + 20 > len(data):  # HasLinkInfo
        return None
    info_start = offset
    ansi_off = struct.unpack_from("<I", data, info_start + 12)[0]      # LocalBasePathOffset
    unicode_off = struct.unpack_from("<I", data, info_start + 16)[0]   # LocalBasePathOffsetUnicode
    for off, encoding in ((unicode_off, "utf-16-le"), (ansi_off, "mbcs")):
        if not off:
            continue
        pos = info_start + off
        if pos >= len(data):
            continue
        raw = data[pos:]
        if encoding == "utf-16-le":
            # 按字符找 null（不能 find(b"\x00\x00")：会把 "e\x00\x00\x00" 错配提前 1 字节）
            text = raw.decode("utf-16-le", errors="ignore")
            end = text.find("\x00")
            if end <= 0:
                continue
            target = text[:end]
        else:
            end = raw.find(b"\x00")
            if end <= 0:
                continue
            target = raw[:end].decode(encoding, errors="ignore")
        if _is_valid_target(target):
            return target
    return None


def _scan_lnk_abs_path(data: bytes) -> str | None:
    """全文扫描兜底：按 UTF-16LE 与 ANSI 两种编码扫描「盘符冒号反斜杠」绝对路径，取最后一条合法目标。"""
    candidates = []
    for drive in range(ord("A"), ord("Z") + 1):
        # UTF-16LE：C\x00:\x00\\\x00
        marker16 = bytes([drive]) + b"\x00:\x00\\\x00"
        pos = data.find(marker16)
        while pos >= 0:
            end = pos + len(marker16)
            while end + 2 <= len(data) and data[end:end + 2] != b"\x00\x00":
                end += 2
            raw = data[pos:end]
            if len(raw) % 2:  # 去掉可能的半个 UTF-16 单元
                raw = raw[:-1]
            path = raw.decode("utf-16-le", errors="ignore")
            if _is_valid_target(path):
                candidates.append(path)
            pos = data.find(marker16, pos + 1)
        # ANSI：C:\
        marker = bytes([drive]) + b":\\"
        pos = data.find(marker)
        while pos >= 0:
            end = data.find(b"\x00", pos)
            if end < 0:
                end = len(data)
            path = data[pos:end].decode("mbcs", errors="ignore")
            if _is_valid_target(path):
                candidates.append(path)
            pos = data.find(marker, pos + 1)
    return candidates[-1] if candidates else None


def _resolve_process_name(path: str) -> str | None:
    """从目标路径解析进程名（用于 taskkill）：exe 直接取文件名，.lnk 先解析目标。"""
    lower = path.lower()
    if lower.endswith(".lnk"):
        target = _parse_lnk_target(path)
        if not target:
            return None
        return os.path.basename(target)
    return os.path.basename(path)


def _process_running(proc_name: str) -> bool:
    """用 tasklist 检查同名进程是否存在。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/NH"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc_name.lower() in result.stdout.lower()


def _taskkill(proc_name: str, force: bool) -> bool:
    """强杀/温和结束同名进程；taskkill 返回 0 表示成功。"""
    cmd = ["taskkill", "/IM", proc_name]
    if force:
        cmd.append("/F")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def close_app(name: str, matcher=None) -> tuple[bool, str]:
    """
    关闭应用：find_app 定位路径（含语义兜底）→ 解析进程名 → 强杀。
    未在运行返回"没在运行"（正常语义）；找不到/解析失败/杀不掉返回对应提示。
    """
    target = find_app(name, matcher=matcher)
    if not target:
        return False, OPEN_NOT_FOUND_TEMPLATE.format(name=name)
    proc_name = _resolve_process_name(target)
    if not proc_name:
        logger.warning("无法从路径解析进程名：%s", target)
        return False, CLOSE_FAILED_TEMPLATE.format(name=name)
    if not _process_running(proc_name):
        logger.info("应用未在运行，无需关闭：%s（%s）", name, proc_name)
        return True, CLOSE_NOT_RUNNING_TEMPLATE.format(name=name)
    if _taskkill(proc_name, force=True):
        logger.info("已关闭应用：%s（进程 %s）", name, proc_name)
        return True, CLOSE_SUCCESS_TEMPLATE.format(name=name)
    logger.warning("关闭应用失败：%s（进程 %s）", name, proc_name)
    return False, CLOSE_FAILED_TEMPLATE.format(name=name)
