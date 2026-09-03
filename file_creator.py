# file_creator.py
# 新建文件（模型意图 create_file 的执行层）。
#
# 安全设计：检查位置 = 写入位置 = 实际落盘位置，三者强制一致。
# 校验链：
#   1) 归一化：只认「盘符 + 分隔符」开头的完整路径；normpath 解析 ..，
#      realpath 解析符号链接（junction 逃逸 → 盘符检查兜住）；
#   2) 盘符：必须 E/e，其余（含 UNC、设备路径）一律拒绝；
#   3) 已存在：文件或同名目录存在即拒绝，绝不覆盖；
#   4) 父目录：必须已存在，不自动创建；
#   5) 逃逸堵漏：拒绝 \\?\ 等绕过规范化的前缀；路径中除盘符外不得再含冒号
#      （拦 NTFS 备用数据流与 E:\C:\ 怪路径）；剥离尾部点/空格（对齐 NTFS 剥离
#      语义，防"检查不存在、实际写已有文件"）；写入用 x 独占模式，
#      FileExistsError 映射为"已存在"文案（堵最后一道竞态）。

import logging
import os
import re

from utils import estimate_tokens

logger = logging.getLogger("jingjing.file_creator")

# 允许新建文件的盘符集合：A/B/D/E/F/G/H 均可，唯独排除 C 盘（不区分大小写；测试可 monkeypatch）
ALLOWED_DRIVES = frozenset("abdefgh")

# 内容长度上限（token，与意图理解预算 INTENT_MAX_TOKENS 一致）
CREATE_FILE_MAX_CONTENT_TOKENS = 1024

# -------- 面向用户的文案（鲸鲸口吻） --------
MSG_NOT_D_DRIVE = "呜～主人，鲸鲸只能在 A/B/D/E/F/G/H 盘新建文件哦～"
MSG_EXISTS = "主人，这个位置已经有文件或文件夹啦，鲸鲸不会覆盖的～"
MSG_NO_PARENT = "呜～主人，那个文件夹还不存在呢，鲸鲸不会自动创建的～"
MSG_RELATIVE_PATH = "呜～主人，请给鲸鲸一个完整的盘符路径，比如 E:/workplace/xxx.txt～"
MSG_BAD_PATH = "呜～主人，这个路径鲸鲸看不懂呢，换一个试试？"
MSG_CONTENT_TOO_LONG = "呜～主人，要写的内容太长啦，鲸鲸一次写不下，精简一点好不好？"
MSG_WRITE_FAILED = "呜～主人，文件没能写进去，检查一下权限或者换个位置试试吧～"
MSG_SUCCESS = "主人，文件已经建好啦～ 内容也写进去咯！"

# 绕过规范化的路径前缀（Win32 设备/全局路径），一律拒绝
_BYPASS_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")

# 完整路径 = 盘符 + 分隔符（normpath 后分隔符为反斜杠）
_DRIVE_ABS_RE = re.compile(r"^([A-Za-z]):[\\/]")
# 有盘符但盘符后不是分隔符（驱动器相对路径，如 e:..\x、E:foo）→ 位置取决于 CWD，不可预期
_DRIVE_REL_RE = re.compile(r"^[A-Za-z]:")


def normalize_create_path(raw: str) -> tuple[str | None, str | None]:
    """
    归一化并校验用户路径，返回 (归一化后的绝对物理路径, 失败文案)。
    成功时失败文案为 None；任何一步不过直接返回对应文案，绝不继续。
    """
    text = (raw or "").strip().strip("\"'“”‘’")
    if not text:
        return None, MSG_BAD_PATH

    # 绕过规范化的前缀（\\?\ \\.\ //?/ //./）→ 不信任
    if text.startswith(_BYPASS_PREFIXES):
        logger.warning("拒绝绕过规范化的路径前缀：%r", text[:30])
        return None, MSG_BAD_PATH

    # 前置格式检查：必须是「盘符 + 分隔符」开头的完整路径
    if not re.match(r"^[A-Za-z]:[\\/]", text):
        if _DRIVE_REL_RE.match(text):
            # e:..\x / E:foo → 驱动器相对路径，位置随 E 盘 CWD 飘移
            return None, MSG_RELATIVE_PATH
        if text.startswith(("\\\\", "//")):
            # UNC 路径：没有盘符，不在 D 盘
            return None, MSG_NOT_D_DRIVE
        # 无盘符的相对路径
        return None, MSG_RELATIVE_PATH

    # normpath 解析 .. 与分隔符 → realpath 解析符号链接（真实物理路径）
    norm = os.path.normpath(text)
    real = os.path.realpath(norm)
    # Windows 的 realpath 可能返回 \\?\ 系统前缀（GetFinalPathNameByHandleW 的规范形式），
    # 还原为普通绝对路径；UNC 目标还原为 \\server\share 形式
    if real.startswith("\\\\?\\"):
        rest = real[len("\\\\?\\"):]
        real = ("\\\\" + rest) if rest.lower().startswith("unc\\") else rest
    path = os.path.normpath(real)

    # 归一化后仍必须是「盘符 + 分隔符」形式（realpath 可能因符号链接改变盘符）
    match = _DRIVE_ABS_RE.match(path)
    if not match:
        return None, MSG_BAD_PATH

    # 盘符必须在允许集合内（A/B/D/E/F/G/H；junction 指向 C 盘等逃逸在这里被拦下）
    if match.group(1).lower() not in ALLOWED_DRIVES:
        return None, MSG_NOT_D_DRIVE

    # 除盘符冒号外不得再含冒号（拦 NTFS 备用数据流 file.txt:hidden、E:\C:\ 怪路径）
    if ":" in path[2:]:
        return None, MSG_BAD_PATH

    # 剥离尾部点/空格（对齐 NTFS 剥离语义，避免检查与落盘位置不一致）
    stripped = path.rstrip(". ")
    if not stripped or not _DRIVE_ABS_RE.match(stripped):
        return None, MSG_BAD_PATH
    # 盘符统一大写（Windows 不区分大小写；保证日志/校验一致）
    return stripped[0].upper() + stripped[1:], None


def create_file(path: str, content: str) -> tuple[bool, str]:
    """
    新建文件：归一化 → 盘符 D → 已存在拒绝 → 父目录必须存在 → x 独占模式 UTF-8 写入。
    返回 (是否成功, 面向用户的文案)。
    """
    normalized, err = normalize_create_path(path)
    if err is not None:
        return False, err

    # 内容长度校验（超限拒绝，避免模型截断内容造成静默损坏）
    if estimate_tokens(content) > CREATE_FILE_MAX_CONTENT_TOKENS:
        return False, MSG_CONTENT_TOO_LONG

    # 已存在（文件或同名目录）→ 拒绝，绝不覆盖
    if os.path.exists(normalized):
        return False, MSG_EXISTS

    # 父目录必须已存在（不自动创建）
    parent = os.path.dirname(normalized)
    if not os.path.isdir(parent):
        return False, MSG_NO_PARENT

    try:
        # x 独占模式：文件已存在时原子失败（竞态兜底，绝不覆盖）
        with open(normalized, "x", encoding="utf-8", newline="") as f:
            f.write(content)
    except FileExistsError:
        return False, MSG_EXISTS
    except (OSError, ValueError) as e:
        logger.warning("新建文件失败（%s）：%s", normalized, e)
        return False, MSG_WRITE_FAILED

    logger.info("新建文件成功：%s（%d 字符）", normalized, len(content))
    return True, MSG_SUCCESS
