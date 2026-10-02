"""课程知识库（RAG）：全量课程产物 → BM25 检索索引 → 供 agent/界面检索。

设计：
- 纯标准库 BM25（中文按 utils.tokenize 分词），零第三方依赖；
- 数据源：output 课程目录的 sections.json（知识点/拓展/习题，含答案）；
- 索引持久化为 data/kb_index.json，可随时增量重建（幂等）；
- 检索结果带完整元数据（课程/章节/类型/时间），供调用方引用。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from .utils import tokenize

_K1 = 1.5
_B = 0.75
_TYPES = ("knowledge", "expansion", "exercise")


def iter_course_chunks(course_root: str | Path) -> list[dict]:
    """扫描课程目录，抽取知识块。每块自带检索文本与元数据。"""
    root = Path(course_root)
    chunks: list[dict] = []
    if not root.is_dir():
        return chunks
    for d in sorted(p for p in root.iterdir() if (p / "sections.json").is_file()):
        try:
            secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
            session = json.loads((d / "session.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        course = session.get("title") or d.name
        cid = d.name
        for si, sec in enumerate(secs):
            stitle = sec.get("title", "")
            for kp in sec.get("knowledge_points", []):
                text = " ".join(filter(None, [
                    kp.get("title", ""), kp.get("concept") or kp.get("detail", ""),
                    kp.get("example", ""), kp.get("pitfall", "")]))
                if not text.strip():
                    continue
                chunks.append({
                    "type": "knowledge", "course": course, "course_id": cid,
                    "section": stitle, "title": kp.get("title", ""),
                    "text": text, "time": kp.get("time", ""),
                    "concept": kp.get("concept") or kp.get("detail", ""),
                    "example": kp.get("example", ""), "pitfall": kp.get("pitfall", "")})
            for ex in sec.get("expansions", []):
                text = f"{ex.get('topic', '')} {ex.get('content', '')}"
                if not text.strip():
                    continue
                chunks.append({
                    "type": "expansion", "course": course, "course_id": cid,
                    "section": stitle, "title": ex.get("topic", ""),
                    "text": text, "time": "",
                    "concept": ex.get("content", "")})
            for ei, e in enumerate(sec.get("exercises", [])):
                text = " ".join(filter(None, [
                    e.get("question", ""), " ".join(e.get("options") or []),
                    e.get("answer", ""), e.get("explanation", "")]))
                if not text.strip():
                    continue
                chunks.append({
                    "type": "exercise", "course": course, "course_id": cid,
                    "section": stitle, "title": e.get("question", "")[:60],
                    "text": text, "time": "",
                    "question": e.get("question", ""), "answer": e.get("answer", ""),
                    "explanation": e.get("explanation", ""), "etype": e.get("type", ""),
                    "difficulty": e.get("difficulty", "")})
    return chunks


class KnowledgeBase:
    """BM25 检索索引（JSON 持久化）。"""

    def __init__(self, index_path: str | Path):
        self.path = Path(index_path)
        self.chunks: list[dict] = []
        self.postings: dict[str, list[list]] = {}
        self.doc_len: list[int] = []
        self.avgdl = 0.0

    # ------------------------------------------------------------ build/save
    def build(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.postings = {}
        self.doc_len = []
        total = 0
        for i, ch in enumerate(chunks):
            tokens = list(tokenize(ch.get("text", "") + " " + ch.get("title", "")))
            self.doc_len.append(len(tokens))
            total += len(tokens)
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            for t, f in tf.items():
                self.postings.setdefault(t, []).append([i, f])
        self.avgdl = (total / len(chunks)) if chunks else 0.0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"chunks": self.chunks, "postings": self.postings,
             "doc_len": self.doc_len, "avgdl": self.avgdl},
            ensure_ascii=False), encoding="utf-8")

    def load(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.chunks = d["chunks"]
            self.postings = d["postings"]
            self.doc_len = d["doc_len"]
            self.avgdl = d["avgdl"]
            return True
        except (json.JSONDecodeError, KeyError, OSError):
            return False

    # --------------------------------------------------------------- search
    def search(self, query: str, top_k: int = 5,
               type_: Optional[str] = None) -> list[dict]:
        if not self.chunks:
            return []
        qtokens = list(tokenize(query))
        scores: dict[int, float] = {}
        n = len(self.chunks)
        for t in set(qtokens):
            postings = self.postings.get(t)
            if not postings:
                continue
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, f in postings:
                s = idf * f * (_K1 + 1) / (f + _K1 * (1 - _B + _B * self.doc_len[i] / self.avgdl))
                scores[i] = scores.get(i, 0.0) + s
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        out = []
        for i, s in ranked:
            if type_ and self.chunks[i].get("type") != type_:
                continue
            ch = dict(self.chunks[i])
            ch["score"] = round(s, 3)
            out.append(ch)
            if len(out) >= top_k:
                break
        return out


def build_kb(course_root: str | Path, index_path: str | Path) -> KnowledgeBase:
    kb = KnowledgeBase(index_path)
    kb.build(iter_course_chunks(course_root))
    kb.save()
    return kb
