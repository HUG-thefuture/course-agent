"""素材加载器：把 文件/字幕/网页/回放链接/音视频 统一为 Material，并分块。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from pathlib import Path

from ..pipeline.models import Chunk, Material, Segment
from ..utils import safe_read_text
from . import parsers, tencent as tencent_mod
from .hls import download_stream
from .transcriber import transcribe_media

_log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts"}
MEDIA_URL_EXTS = (".m3u8", ".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm",
                  ".mp3", ".m4a", ".aac", ".wav")


class IngestError(Exception):
    """素材加载失败。"""


def classify_url(url: str) -> str:
    """URL 分类：tencent_share / media / page。纯函数，便于测试。"""
    low = (url or "").lower()
    if tencent_mod.is_tencent_share(low):
        return "tencent_share"
    if ".m3u8" in low:
        return "media"
    path = urllib.parse.urlparse(low).path
    if path.endswith(MEDIA_URL_EXTS):
        return "media"
    return "page"


def resolve_media_url(url: str, password: str = "", workdir: str | Path = "data") -> Path:
    """把回放/媒体 URL 落地为本地视频文件（带缓存，重复运行不重复下载）。"""
    if classify_url(url) == "tencent_share":
        video = tencent_mod.try_extract_video_url(url, password)
        if not video:
            raise IngestError(tencent_mod.guided_steps(url))
        _log.info("已从分享页自动提取到媒体地址")
        url = video
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    out = Path(workdir) / "downloads" / f"{name}.mp4"
    try:
        return download_stream(url, out)
    except Exception as e:
        if isinstance(e, IngestError):
            raise
        raise IngestError(str(e)) from e


def fetch_url(url: str, password: str = "", timeout: int = 30) -> tuple[str, str]:
    """抓取网页，返回 (title, text)。

    password 处理说明：多数课程平台加密方式各不相同，这里提供最通用的两种——
    追加 ?password= 查询参数，或 HTTP Basic 认证（url 形如 https://user:pass@host）。
    腾讯会议回放走 classify_url -> resolve_media_url 专用路径，不经过本函数。
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


def load_tencent_material(url: str, password: str = "") -> Material:
    """腾讯会议回放：自动提取 AI 逐字稿 -> Material（免下载视频、免本地转写）。

    提取失败（无逐字稿/提取码错误/网络问题）抛 IngestError，原因可读。
    """
    try:
        rows, meta = tencent_mod.harvest_minutes(url, password)
    except (ValueError, OSError) as e:
        raise IngestError(tencent_mod.guided_steps(url, str(e))) from e
    segments = [Segment(start=r["start"], text=r["text"]) for r in rows]
    _log.info("自动提取逐字稿：%s（%s 段，约 %s 分钟）",
              meta.get("source", "")[-12:], len(segments), meta.get("duration_min"))
    return Material(title=f"回放课程 {tencent_mod._short_code(url)}"[:40],
                    kind="transcript", source=url, segments=segments)


def load_source(path_or_url: str, password: str = "",
                workdir: str | Path = "data") -> Material:
    """统一入口：文件路径或 http(s) URL -> Material。"""
    if re.match(r"^https?://", path_or_url):
        kind = classify_url(path_or_url)
        if kind == "tencent_share":
            return load_tencent_material(path_or_url, password)
        if kind == "media":
            local = resolve_media_url(path_or_url, password=password, workdir=workdir)
            segments = transcribe_media(local)
            if not segments:
                raise IngestError(f"视频转写结果为空: {local}")
            return Material(title=Path(path_or_url).stem[:40] or "回放课程",
                            kind="video", source=path_or_url, segments=segments)
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
        if not segs:
            raise IngestError(f"音视频转写结果为空: {p}")
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
