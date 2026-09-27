"""解析器：SRT/VTT 字幕、HTML 网页正文提取。均为纯函数，便于测试。"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..pipeline.models import Segment

# 可选小时位的通用时间戳：00:01:02,000 --> 00:01:04,500 或 01:02.500 --> ...
_TS_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)

_SKIP_TAGS = {"script", "style", "noscript", "template"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "tr", "section", "article", "blockquote", "pre"}


def _to_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    hours = int(h) if h else 0
    return hours * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_subtitle(text: str) -> list[Segment]:
    """解析 SRT/VTT 字幕为带时间戳的 Segment 列表。"""
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        m = _TS_RE.search(block)
        if not m:
            continue  # 跳过纯序号块或 WEBVTT 头
        start = _to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        body_lines = []
        seen_ts = False
        for ln in lines:
            if _TS_RE.search(ln):
                seen_ts = True
                continue
            if not seen_ts and re.fullmatch(r"\d+", ln):
                continue  # SRT 序号
            if ln in ("WEBVTT",) or ln.startswith(("NOTE", "STYLE")):
                continue
            body_lines.append(re.sub(r"<[^>]+>", "", ln))
        body = " ".join(body_lines).strip()
        if body:
            segments.append(Segment(start=start, text=body))
        _ = end  # 保留解析但当前只按起点分块
    return segments


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        elif self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def extract_html(raw: str) -> tuple[str, str]:
    """HTML -> (title, 正文文本)。纯标准库实现，无第三方依赖。"""
    ex = _TextExtractor()
    try:
        ex.feed(raw)
        ex.close()
    except Exception:
        pass  # 容忍残缺 HTML，提取到多少算多少
    text = "".join(ex.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return ex.title.strip(), text.strip()


def plain_to_segments(text: str) -> list[Segment]:
    """纯文本按段落/句子切成 Segment（无时间戳）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:
        parts = [p.strip() for p in re.split(r"(?<=[。！？!?])\s*", text) if p.strip()]
        paras = [" ".join(parts[i:i + 3]) for i in range(0, len(parts), 3)]
        paras = [p for p in paras if p.strip()]
    return [Segment(start=None, text=p) for p in paras]
