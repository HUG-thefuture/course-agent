"""批量课程处理：目录 / 清单文件 / 多个链接，逐个 解析->转写->整理。

设计要点：
- 逐项隔离：单个课程失败只记录，不中断整批；
- 断点续跑：已产出 session.json 的课程自动跳过（--force 强制重跑）；
- 转写缓存：同一视频文件只转写一次（按 路径+大小+mtime 做键）；
- 共享记忆库：全批课程的高质量范例互相增强。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import AgentConfig
from .ingest.loader import (AUDIO_EXTS, IngestError, VIDEO_EXTS,
                            classify_url, load_source, resolve_media_url)
from .ingest.transcriber import transcribe_media
from .llm.base import BaseLLM
from .memory import Memory
from .outputs import write_all
from .pipeline.models import Material, Segment
from .pipeline.orchestrator import Orchestrator
from .utils import safe_read_text, slugify, truncate

_log = logging.getLogger(__name__)

_MANIFEST_EXTS = {".json", ".list"}   # 清单文件专用扩展名（.txt/.md 单文件视为课程文稿）
_SUBTITLE_EXTS = {".srt", ".vtt"}
_TRANSCRIPT_EXTS = {".txt", ".md", ".markdown", ".json"}  # .json = 转写稿（loader json 格式）
_MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
_SCAN_EXTS = _MEDIA_EXTS | _SUBTITLE_EXTS | _TRANSCRIPT_EXTS


@dataclass
class BatchItem:
    source: str
    title: str = ""
    password: str = ""


@dataclass
class BatchOutcome:
    source: str
    title: str
    status: str  # passed / qa_failed / failed / skipped
    session_id: str = ""
    score: int = 0
    error: str = ""
    output_dir: str = ""


def discover_items(inputs: list[str]) -> list[BatchItem]:
    """把批量输入（目录/清单/URL/单文件）展开为 BatchItem 列表。"""
    items: list[BatchItem] = []
    for inp in inputs:
        if re.match(r"^https?://", inp):
            items.append(BatchItem(source=inp))
            continue
        p = Path(inp)
        if p.is_dir():
            found = [f for f in sorted(p.iterdir())
                     if f.is_file() and f.suffix.lower() in _SCAN_EXTS
                     and not f.name.startswith(".")
                     and not f.name.startswith(("batch_report", "harvest_report"))]
            for f in found:
                items.append(BatchItem(source=str(f), title=f.stem))
            if not found:
                _log.warning("目录中没有可识别的课程素材: %s", p)
        elif p.is_file() and p.suffix.lower() in _MANIFEST_EXTS:
            if p.suffix.lower() == ".json":
                data = json.loads(safe_read_text(p))
                rows = data if isinstance(data, list) else data.get("courses", [])
                for row in rows:
                    if isinstance(row, str):
                        items.extend(_parse_manifest_line(row))
                    elif isinstance(row, dict) and row.get("source"):
                        items.append(BatchItem(
                            source=str(row["source"]),
                            title=str(row.get("title") or ""),
                            password=str(row.get("password") or "")))
            else:  # .list：每行 `链接|提取码|标题` 或 `路径|标题` 或裸源
                for line in safe_read_text(p).splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        items.extend(_parse_manifest_line(line))
        elif p.is_file() and p.suffix.lower() in (".txt", ".md", ".markdown"):
            # 明确给出的单个文稿 → 课程材料；行格式清单请用 .list 扩展名
            items.append(BatchItem(source=str(p), title=p.stem))
        elif p.is_file():
            items.append(BatchItem(source=str(p), title=p.stem))
        else:
            items.append(BatchItem(source=inp))  # 交给逐项处理时报错，保持隔离
    return items


def _parse_manifest_line(line: str) -> list[BatchItem]:
    """清单行格式：`源 | 提取码 | 标题`（链接）或 `路径 | 标题`（文件）。"""
    parts = [x.strip() for x in line.split("|")]
    if len(parts) >= 3:
        return [BatchItem(parts[0], title=parts[2], password=parts[1])]
    if len(parts) == 2:
        if re.match(r"^https?://", parts[0]):
            return [BatchItem(parts[0], password=parts[1])]
        return [BatchItem(parts[0], title=parts[1])]
    return [BatchItem(parts[0])] if parts and parts[0] else []


def _title_from_source(source: str) -> str:
    if re.match(r"^https?://", source):
        tail = re.sub(r"[?#].*$", "", source).rstrip("/").rsplit("/", 1)[-1]
        return slugify(tail or "课程") or "课程"
    return Path(source).stem or "课程"


def _transcribe_cached(path: Path, cfg: AgentConfig) -> list[Segment]:
    """带缓存的转写：同一文件（路径+大小+mtime）只转写一次。

    空结果不缓存（否则瞬时转写异常被放大为永久失败）；缓存文件损坏视为未命中。
    """
    stat = path.stat()
    key = hashlib.sha1(
        f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode()).hexdigest()[:16]
    cache = Path(cfg.data_dir) / "transcript_cache" / f"{key}.json"
    if cache.is_file():
        try:
            rows = json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            rows = None
        if rows is not None:
            _log.info("命中转写缓存: %s", cache.name)
            return [Segment(start=r.get("start"), text=r["text"])
                    for r in rows if r.get("text")]
    segs = transcribe_media(path)
    if not segs:
        return segs
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(
        json.dumps([{"start": s.start, "text": s.text} for s in segs],
                   ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache)
    return segs


def _material_for_item(item: BatchItem, cfg: AgentConfig) -> Material:
    """把单个批量项解析为 Material（视频自动下载+转写，带缓存）。"""
    src = item.source
    if re.match(r"^https?://", src):
        if classify_url(src) in ("tencent_share", "media"):
            local = resolve_media_url(src, password=item.password,
                                      workdir=cfg.data_dir)
            segments = _transcribe_cached(local, cfg)
            if not segments:
                raise IngestError(f"视频转写结果为空: {local}")
            return Material(title=item.title or "回放课程", kind="video",
                            source=src, segments=segments)
        return load_source(src, password=item.password)  # 普通网页

    p = Path(src)
    if not p.is_file():
        raise IngestError(f"文件不存在: {p}")
    if p.suffix.lower() in _MEDIA_EXTS:
        segments = _transcribe_cached(p, cfg)
        if not segments:
            raise IngestError(f"音视频转写结果为空: {p}")
        return Material(title=item.title or p.stem, kind="video",
                        source=str(p), segments=segments)
    m = load_source(src)
    if item.title:
        m.title = item.title
    return m


def _process_items(cfg: AgentConfig, items: list[BatchItem],
                   out_root: Path, memory: Memory | None,
                   vision_llm: BaseLLM | None, force: bool,
                   review_llm: BaseLLM | None, orch: Orchestrator,
                   label: str, outdir_of) -> list[BatchOutcome]:
    """处理一批条目（不写报告）。outdir_of(item, index) 返回产物目录。"""
    outcomes: list[BatchOutcome] = []
    total = len(items)
    for i, item in enumerate(items, 1):
        title = item.title or _title_from_source(item.source)
        outdir = outdir_of(item, i)
        if not force and (outdir / "session.json").is_file():
            _log.info("[%s %d/%d] 跳过（已有产物，--force 可重跑）：%s", label, i, total, title)
            outcomes.append(BatchOutcome(item.source, title, "skipped",
                                         output_dir=str(outdir)))
            continue
        _log.info("[%s %d/%d] 开始处理：%s", label, i, total, title)
        try:
            material = _material_for_item(item, cfg)
            course_title = item.title or material.title
            result = orch.run(material, title=course_title)
            result.output_dir = str(outdir)
            write_all(result, outdir)
            outcomes.append(BatchOutcome(
                item.source, course_title,
                "passed" if result.passed else "qa_failed",
                session_id=result.session_id,
                score=result.qa.score if result.qa else 0,
                output_dir=str(outdir)))
            _log.info("[%s %d/%d] 完成：%s（%s，得分 %d）", label, i, total, title,
                      outcomes[-1].status, outcomes[-1].score)
        except Exception as e:  # 单项失败不中断整批
            _log.error("[%s %d/%d] 处理失败：%s -> %s", label, i, total, title, e)
            outcomes.append(BatchOutcome(item.source, title, "failed",
                                         error=truncate(str(e), 300),
                                         output_dir=str(outdir)))
    return outcomes


def run_batch(llm: BaseLLM, cfg: AgentConfig, items: list[BatchItem],
              out_root: str | Path, memory: Memory | None = None,
              vision_llm: BaseLLM | None = None, force: bool = False,
              review_llm: BaseLLM | None = None) -> list[BatchOutcome]:
    """顺序处理全部条目；失败课程按 retry_until_pass 轮数持续重试直至无问题。

    返回逐项结果（重试后以最新结果为准）；汇总写入 out_root/batch_report.json。
    """
    if not items:
        raise ValueError("批量列表为空")
    orch = Orchestrator(llm, cfg, memory=memory, vision_llm=vision_llm,
                        review_llm=review_llm)
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    outcomes = _process_items(
        cfg, items, out_root, memory, vision_llm, force, review_llm, orch,
        label="首轮",
        outdir_of=lambda it, i: out_root / f"{i:02d}-{slugify(it.title or _title_from_source(it.source))}")

    # ---- 持续迭代：失败/未达标课程反复重跑，直至全部通过或轮数用尽 ----
    # outcomes 与 items 按下标一一对应（_process_items 每项恰好产出一条），
    # 重试以索引对齐，避免「标题解析前后不一致」导致失败课程永远匹配不上
    passes_left = max(0, int(cfg.pipeline.retry_until_pass))
    round_no = 0
    while passes_left > 0:
        pending = [(i, o) for i, o in enumerate(outcomes)
                   if o.status in ("failed", "qa_failed")]
        if not pending:
            break
        round_no += 1
        passes_left -= 1
        _log.info("=== 持续迭代第 %d 轮：%d 门未达标，重新生成 ===",
                  round_no, len(pending))
        retry_items = [items[i] for i, _ in pending]
        retry_outcomes = _process_items(
            cfg, retry_items, out_root, memory, vision_llm, force=True,
            review_llm=review_llm, orch=orch, label=f"迭代{round_no}",
            outdir_of=lambda it, j: Path(pending[j - 1][1].output_dir))
        for j, ro in enumerate(retry_outcomes):
            outcomes[pending[j][0]] = ro

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items), **counts,
        "retry_rounds": round_no,
        "outcomes": [asdict(o) for o in outcomes],
    }
    report_path = out_root / "batch_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    _log.info("批量完成：%s（迭代 %d 轮），报告 -> %s", counts, round_no, report_path)
    return outcomes
