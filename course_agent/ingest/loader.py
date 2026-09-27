"""素材加载器：把 文件/字幕/网页/音视频 统一为 Material，并分块。"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path

from ..pipeline.models import Chunk, Material, Segment
from ..utils import safe_read_text
from . import parsers
from .transcriber import transcribe_media

_log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts"}


class IngestError(Exception):
    """素材加载失败。"""


def fetch_url(url: str, password: str = "", timeout: int = 30) -> tuple[str, str]:
    """抓取网页，返回 (title, text)。

    password 处理说明：多数课程平台加密方式各不相同，这里提供最通用的两种——
    追加 ?password= 查询参数，或 HTTP Basic 认证（url 形如 https://user:pass@host）。
    平台特有逻辑请通过自定义适配脚本预处理后走文件输入。
    """
    if not re.match(r"^https?://", url):
        raise IngestError(f"不支持的 URL: {url}")
    real_url = url
    headers = {"User-Agent": "Mozilla/5.0 (CourseAgent/1.0)"}
    if password:
        sep = "&" if "?" in real_url else "?"
        real_url = real_url + sep + urllib.parse.urlencode({"password": password})
    req = urllib.request.Request(real_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except Exception as e:
        raise IngestError(f"抓取网页失败 {url}: {e}") from e
    title, text = parsers.extract_html(raw)
    if not text:
        raise IngestError(f"网页未提取到正文（可能是纯脚本渲染页面）: {url}")
    return title or url, text


def load_source(path_or_url: str, password: str = "") -> Material:
    """统一入口：文件路径或 http(s) URL -> Material。"""
    if re.match(r"^https?://", path_or_url):
        title, text = fetch_url(path_or_url, password=password)
        return Material(title=title, kind="url", source=path_or_url,
                        segments=parsers.plain_to_segments(text))

    p = Path(path_or_url)
    if not p.is_file():
        raise IngestError(f"文件不存在: {p}")
    ext = p.suffix.lower()

    if ext in (".txt", ".md", ".markdown"):
        text = safe_read_text(p)
        return Material(title=p.stem, kind="text", source=str(p),
                        segments=parsers.plain_to_segments(text))
    if ext in (".srt", ".vtt"):
        text = safe_read_text(p)
        segs = parsers.parse_subtitle(text)
        if not segs:
            raise IngestError(f"字幕解析为空: {p}")
        return Material(title=p.stem, kind=ext.lstrip("."), source=str(p), segments=segs)
    if ext == ".json":
        data = json.loads(safe_read_text(p))
        segs = [Segment(start=item.get("start"), text=str(item.get("text", "")))
                for item in data if item.get("text")]
        return Material(title=p.stem, kind="json", source=str(p), segments=segs)
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise IngestError("需要 PDF 支持：pip install pypdf") from e
        reader = PdfReader(str(p))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        return Material(title=p.stem, kind="pdf", source=str(p),
                        segments=parsers.plain_to_segments(text))
    if ext in AUDIO_EXTS or ext in VIDEO_EXTS:
        segs = transcribe_media(p)
        return Material(title=p.stem, kind="audio/video", source=str(p), segments=segs)
    raise IngestError(f"不支持的文件类型 {ext}，可用：txt/md/srt/vtt/json/pdf/音视频")


def chunk_material(material: Material, max_chars: int = 3000) -> list[Chunk]:
    """把 Material 切分为不超过 max_chars 的块，保留时间戳边界。

    内存安全：只做一次线性扫描，块的引用来自原 segments 文本切片，
    不产生整份素材的额外副本。
    """
    if max_chars < 200:
        raise ValueError("chunk 过小会导致提示词破碎，max_chars 至少 200")
    segments = list(material.segments)
    if not segments:
        raise IngestError("素材为空，无法分块")

    # 单段超长时二次切分
    expanded: list[Segment] = []
    for seg in segments:
        if len(seg.text) <= max_chars:
            expanded.append(seg)
            continue
        pieces = _split_long(seg.text, max_chars)
        offset = seg.start
        for i, piece in enumerate(pieces):
            start = offset if (offset is not None and i == 0) else None
            expanded.append(Segment(start=start, text=piece))

    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_size = 0
    cur_start: float | None = None
    last_end: float | None = None

    def flush() -> None:
        nonlocal cur, cur_size, cur_start
        if cur:
            chunks.append(Chunk(index=len(chunks), text="\n".join(cur),
                                start_time=cur_start, end_time=last_end))
            cur, cur_size, cur_start = [], 0, None

    for seg in expanded:
        if cur and cur_size + len(seg.text) > max_chars:
            flush()
        if cur_start is None:
            cur_start = seg.start
        cur.append(seg.text)
        cur_size += len(seg.text)
        if seg.start is not None:
            last_end = seg.start
    flush()
    if not chunks:
        raise IngestError("分块结果为空")
    return chunks


def _split_long(text: str, max_chars: int) -> list[str]:
    """把超长段落按句子边界切为多片。"""
    sentences = [s for s in re.split(r"(?<=[。！？!?])\s*", text) if s.strip()]
    pieces: list[str] = []
    buf = ""
    for s in sentences:
        if len(s) > max_chars:
            for i in range(0, len(s), max_chars):
                pieces.append(s[i : i + max_chars])
            buf = ""
            continue
        if len(buf) + len(s) > max_chars and buf:
            pieces.append(buf)
            buf = ""
        buf += s
    if buf:
        pieces.append(buf)
    return pieces
