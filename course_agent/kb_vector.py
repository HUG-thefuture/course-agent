"""向量知识库：Ollama 嵌入 + ChromaDB + 混合检索（向量+BM25+RRF）。

设计：
- 嵌入：本地 Ollama（nomic-embed-text，768 维），数据不出本机；
- 存储：ChromaDB PersistentClient（data/chromadb）；
- 检索：向量语义 + BM25 关键词，RRF（Reciprocal Rank Fusion）融合；
- 幂等：重建时清空集合重灌，课程更新后重跑即可。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .kb import KnowledgeBase, iter_course_chunks


def chunk_id(course_id: str, section: str, type_: str, title: str) -> str:
    raw = f"{course_id}|{section}|{type_}|{title}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


_STOPWORDS = {"什么是", "怎么", "如何", "什么", "区别", "与", "和", "的", "有",
              "哪些", "为什么", "介绍", "一下", "讲讲", "意思", "告诉我"}


def extract_keywords(query: str) -> str:
    """去掉问句套话，保留核心术语（提升嵌入与检索信噪比）。"""
    out = query
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        out = out.replace(w, " ")
    return " ".join(out.split()) or query


class OllamaEmbedder:
    """本地 Ollama 嵌入客户端（批量）。默认 bge-m3（中文/多语言强）。"""

    def __init__(self, model: str = "bge-m3",
                 base_url: str = "http://localhost:11434", timeout: int = 300):
        self.model = model
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(self.base + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read().decode())
        embs = d.get("embeddings")
        if not embs or len(embs) != len(texts):
            raise RuntimeError(f"Ollama 嵌入返回异常: {len(embs or [])}/{len(texts)}")
        return embs


def _meta_of(chunk: dict) -> dict:
    """ChromaDB metadata 只接受标量；None 剔除，长文截断。"""
    keep = {}
    for k in ("type", "course", "course_id", "section", "title", "time",
              "pitfall", "answer", "explanation", "difficulty"):
        v = chunk.get(k)
        if v is None:
            continue
        s = str(v)
        keep[k] = s[:512]
    return keep


def build_vector_kb(course_root: str | Path, chroma_dir: str | Path,
                    embedder: OllamaEmbedder, batch_size: int = 48,
                    log=print) -> int:
    """课程产物 → 嵌入 → ChromaDB。返回入库块数。"""
    import chromadb

    chunks = iter_course_chunks(course_root)
    for c in chunks:
        c["id"] = chunk_id(c.get("course_id", ""), c.get("section", ""),
                           c.get("type", ""), c.get("title", ""))
    client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        client.delete_collection("course_kb")
    except Exception:
        pass
    col = client.get_or_create_collection(
        "course_kb", metadata={"hnsw:space": "cosine"})

    total = len(chunks)
    done = 0
    for i in range(0, total, batch_size):
        part = chunks[i : i + batch_size]
        embs = embedder.embed([c["text"][:4000] for c in part])
        col.upsert(
            ids=[c["id"] for c in part],
            documents=[c["text"][:2000] for c in part],
            embeddings=embs,
            metadatas=[_meta_of(c) for c in part])
        done += len(part)
        log(f"  向量入库 {done}/{total}")
    log(f"向量知识库构建完成：{total} 块 -> {chroma_dir}")
    return total


def vector_search(chroma_dir: str | Path, embedder: OllamaEmbedder,
                  query: str, top_k: int = 8,
                  type_: Optional[str] = None) -> list[dict]:
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        col = client.get_collection("course_kb")
    except Exception:
        return []
    qemb = embedder.embed([query])[0]
    res = col.query(query_embeddings=[qemb], n_results=min(top_k, col.count()),
                    where={"type": type_} if type_ else None)
    out = []
    for i, cid in enumerate(res["ids"][0]):
        meta = res["metadatas"][0][i]
        dist = res["distances"][0][i]
        out.append({**meta, "id": cid, "score": round(1.0 - dist, 3),
                    "text": (res["documents"][0][i] if res.get("documents") else "")})
    return out


def rrf_fuse(rankedList: list[list[dict]], k: int = 60,
             top_k: int = 8) -> list[dict]:
    """Reciprocal Rank Fusion：多路排名融合为单一排序。"""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for ranked in rankedList:
        for rank, ch in enumerate(ranked):
            key = ch.get("id") or f"{ch.get('course', '')}|{ch.get('title', '')}|{rank}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            cur = items.get(key)
            if cur is None or len(json.dumps(cur, ensure_ascii=False)) < len(
                    json.dumps(ch, ensure_ascii=False)):
                base = dict(ch)
                base.setdefault("id", key)
                items[key] = base
    ranked_keys = sorted(scores, key=lambda x: scores[x], reverse=True)[:top_k]
    out = []
    for key in ranked_keys:
        ch = dict(items[key])
        ch["score"] = round(scores[key], 4)
        out.append(ch)
    return out


def hybrid_search(bm25_kb: KnowledgeBase, chroma_dir: str | Path,
                  embedder: OllamaEmbedder, query: str, top_k: int = 8,
                  type_: Optional[str] = None) -> list[dict]:
    """混合检索：BM25 关键词 + 向量语义（关键词提取后）→ RRF 融合。

    BM25 榜权重 1.3（精确术语命中更可信），向量榜负责语义泛化。
    """
    core = extract_keywords(query)
    bm = bm25_kb.search(query, top_k=top_k * 2, type_=type_)
    bm += [c for c in bm25_kb.search(core, top_k=top_k, type_=type_)
           if c not in bm]
    vec = vector_search(chroma_dir, embedder, core or query,
                        top_k=top_k * 2, type_=type_)
    fused = rrf_fuse([bm, vec], top_k=top_k)
    # 向量命中的块可能只有 chroma 精简元数据——从 BM25 块库补全富字段
    rich = {c.get("id"): c for c in bm25_kb.chunks}
    for ch in fused:
        rid = ch.get("id")
        if rid and rid in rich:
            for k, v in rich[rid].items():
                ch.setdefault(k, v)
    return fused


def build_all(course_root: str | Path, data_dir: str | Path,
              embedder: OllamaEmbedder, log=print) -> dict:
    """一键重建：BM25 索引 + 向量库。"""
    data_dir = Path(data_dir)
    bm = KnowledgeBase(data_dir / "kb_index.json")
    bm.build(iter_course_chunks(course_root))
    bm.save()
    n_vec = build_vector_kb(course_root, data_dir / "chromadb", embedder, log=log)
    return {"bm25_chunks": len(bm.chunks), "vector_chunks": n_vec}
