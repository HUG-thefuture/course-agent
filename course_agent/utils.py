"""通用工具：容错 JSON 提取、文本读取、分词、时间格式化、日志。"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

_log = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """初始化日志。Windows 控制台强制 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def safe_read_text(path: str | Path) -> str:
    """按 utf-8-sig / utf-8 / gb18030 顺序尝试读取文本文件。

    utf-8-sig 首位：带 BOM 的文件剥掉 BOM，不带 BOM 的文件解码结果与 utf-8 一致；
    中文 Windows 下常见 GBK 文件，逐一尝试并返回首个成功结果。
    """
    p = Path(path)
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError as e:
            last_err = e
        except OSError:
            raise
    raise ValueError(f"无法解码文件 {p}（尝试过 utf-8/utf-8-sig/gb18030）: {last_err}")


def extract_json(text: str):
    """从大模型输出中稳健地提取第一个完整 JSON 值（对象或数组）。

    处理 markdown 代码围栏、前后杂文、字符串内花括号等情况。
    解析失败抛 ValueError。
    """
    if not text or not text.strip():
        raise ValueError("模型输出为空，无法提取 JSON")
    t = text.strip()
    # 思考型模型（如 Qwen3/DeepSeek-R1）会输出思考段，剥离后再提取
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip() or t
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*[ \t]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    # 取出现位置最早的那个括号作为起点（避免数组内含对象时误从内层 { 开始）
    starts = [(t.find("{"), "{", "}"), (t.find("["), "[", "]")]
    starts = [s for s in starts if s[0] != -1]
    if not starts:
        try:
            return json.loads(t)
        except json.JSONDecodeError as e:
            raise ValueError(f"未找到有效 JSON: {e}") from e
    start, opener, closer = min(starts, key=lambda x: x[0])
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = t[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError as e:
                        raise ValueError(f"JSON 解析失败: {e}") from e
    # 没找到平衡结构，退化为整体解析
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(f"未找到有效 JSON: {e}") from e


_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """中英混合轻量分词：拉丁词 + 中文单字+相邻双字组合。

    返回列表（保留重复项），调用方需要集合语义时自行 set()；
    BM25 等需要词频的场景依赖重复项。
    """
    if not text:
        return []
    low = text.lower()
    toks: list[str] = []
    for w in re.findall(r"[a-z0-9]+", low):
        toks.append(w)
    cjk = re.findall(r"[\u4e00-\u9fff]", low)
    toks.extend(cjk)
    toks.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return toks


def similarity(query: str, target: str) -> float:
    """基于词元交集的简单相似度（0~1），无需额外依赖。"""
    q = set(tokenize(query))
    t = set(tokenize(target))
    if not q or not t:
        return 0.0
    return len(q & t) / min(len(q), len(t))


def fmt_time(seconds: float | int | None) -> str:
    """把秒数格式化为 MM:SS 或 H:MM:SS；None 返回空串。"""
    if seconds is None:
        return ""
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def slugify(title: str, max_len: int = 40) -> str:
    """把标题转成安全的目录名，保留中文与常用字符。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title.strip()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s or "course")[:max_len]


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
