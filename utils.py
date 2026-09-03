# utils.py
# 通用工具：图片编码、MIME 类型、/image 命令解析、情绪检测与立绘文件选择、文本转富文本

import base64
import html
import os
import random
import re
import shlex

from config import MOOD_FILE_MAP, VALID_MOODS

MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
    ".bmp": "image/bmp", ".gif": "image/gif",
}

DEFAULT_QUESTION = "请描述这张图片的内容。"

# 富文本链接：URL 包成可点击 <a>（颜色与界面风格一致）
URL_LINK_RE = re.compile(r"(https?://[^\s<>\"'）】]+)")


def plain_to_html(text: str) -> str:
    """纯文本转富文本：转义 HTML 特殊字符，URL 自动变成可点击链接。"""
    escaped = html.escape(text)
    return URL_LINK_RE.sub(r'<a href="\1" style="color:#007AFF;">\1</a>', escaped)


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数（未安装 tiktoken 时的启发式替代）：
    中文 ≈ 1 token/字，英文 ≈ 4 字符/token。用于上下文窗口预算。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


def encode_image(image_path: str) -> str:
    """本地图片转 Base64 字符串"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_image_mime_type(file_path: str) -> str:
    """根据扩展名获取图片 MIME 类型（未知扩展名默认 JPEG）"""
    ext = os.path.splitext(file_path)[-1].lower()
    return MIME_MAP.get(ext, "image/jpeg")


def process_image_command(raw_cmd: str) -> tuple[str | None, str | None]:
    """
    解析 /image 后面的参数。
    返回: (图片路径, 问题文本)；参数为空时返回 (None, None)。
    """
    try:
        parts = shlex.split(raw_cmd, posix=False)
    except ValueError:
        parts = raw_cmd.split()

    if not parts:
        return None, None

    image_path = parts[0]
    question = " ".join(parts[1:]) if len(parts) > 1 else DEFAULT_QUESTION
    return image_path, question


# ===== 情绪检测 =====

def _clean_mood(raw: str) -> str:
    """去掉情绪词周围的中英文标点"""
    return re.sub(r"[，,。；;！!？?]", "", raw.strip())


def extract_mood_from_tag(text: str) -> str | None:
    """
    从回复文本中提取 (情绪名) 标签（兼容英文/中文括号）。
    优先匹配最后一行，避免误读正文。
    """
    if not text:
        return None

    last_line = text.strip().split("\n")[-1]
    patterns = [
        r"\(([^（）]+)\)\s*$",   # (开心) 在行尾
        r"（([^（）]+)）\s*$",   # （开心）在行尾
    ]

    # 优先看最后一行
    for pattern in patterns:
        match = re.search(pattern, last_line)
        if match:
            mood = _clean_mood(match.group(1))
            if mood in VALID_MOODS:
                return mood

    # 最后一行没找到时，在整个文本里搜索
    for pattern in patterns:
        for raw in re.findall(pattern, text):
            mood = _clean_mood(raw)
            if mood in VALID_MOODS:
                return mood

    return None


# 情绪关键词计分表：命中关键词按出现次数计分，得分最高者胜出；
# 同分时按 MOOD_PRIORITY 顺序（越靠前优先级越高）。
# 注意：人设提示词让鲸鲸常带"哼！"“呜～”等语气词，所以"哼！"不作为生气触发词，
# 否则立绘会一直停在生气；"等"过于常见也不作为期待触发词。
# "人家"是人设口头禅（几乎每句出现），不能作情绪判据（否则卖萌粘滞压过其它情绪），
# 故只保留"撒娇/呜～/抱抱"等更明确的卖萌词。
MOOD_PRIORITY = [
    "生气", "抱歉", "傲娇", "害羞", "伸懒腰", "犯困",
    "惊讶", "怀疑", "期待", "关心", "思考", "卖萌", "开心",
]

MOOD_KEYWORDS = {
    "生气": ("生气", "胖"),
    "抱歉": ("对不起", "抱歉"),
    "傲娇": ("得意", "骄傲", "哼"),
    "害羞": ("害羞", "结巴", "主、主人", "脸红"),
    "伸懒腰": ("伸懒腰", "累了"),
    "犯困": ("困了", "睡觉", "哈欠"),
    "惊讶": ("惊讶", "竟然", "真的吗"),
    "怀疑": ("怀疑", "真的假的", "不信"),
    "期待": ("期待", "盼望"),
    "关心": ("关心", "担心", "心疼"),
    "思考": ("思考", "让我想想", "嗯...", "嗯……"),
    "卖萌": ("撒娇", "呜～", "抱抱"),
    "开心": ("开心", "高兴", "喜欢", "夸奖", "嘿嘿", "嘻嘻", "哈哈", "想你"),
}


def detect_mood_by_keywords(text: str) -> str:
    """
    关键词计分方案：统计各情绪命中的关键词出现次数，得分最高者胜出；
    同分时按 MOOD_PRIORITY 顺序（越靠前优先级越高）。无命中返回“默认”。
    """
    text = text.strip()
    if not text:
        return "默认"

    scores = {
        mood: sum(text.count(keyword) for keyword in keywords)
        for mood, keywords in MOOD_KEYWORDS.items()
    }
    hits = {mood: score for mood, score in scores.items() if score > 0}
    if not hits:
        return "默认"
    return max(hits, key=lambda mood: (hits[mood], -MOOD_PRIORITY.index(mood)))


def detect_final_mood(text: str) -> str:
    """
    最终回复检测：优先提取 AI 标签 (情绪名)，若没有则回退到关键词匹配。
    """
    mood = extract_mood_from_tag(text)
    if mood and mood in VALID_MOODS:
        return mood
    return detect_mood_by_keywords(text)


def get_portrait_path(mood: str, folder: str) -> str:
    """
    根据情绪获取立绘文件的完整路径（值为列表时随机二选一）。
    """
    file_entry = MOOD_FILE_MAP.get(mood, MOOD_FILE_MAP["默认"])
    filename = random.choice(file_entry) if isinstance(file_entry, list) else file_entry
    return os.path.join(folder, filename)
