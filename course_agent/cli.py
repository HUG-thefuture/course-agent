"""命令行入口。

子命令：
  run         处理一份课程材料（文件/URL/腾讯会议回放），产出知识点笔记/关键节点/习题
  fetch       只做 下载回放 -> 本地转写（srt/txt），先验证素材可用再整理
  demo        离线演示（内置 Mock 模型 + 示例字幕，无需配置模型）
  improve     在样例材料上运行自改进循环（生成->质检->沉淀范例）
  rate        对某次会话的章节打分，反馈进入记忆库
  export-sft  把范例库导出为 Alpaca JSONL（用于真正的 LoRA 微调）
  config      查看配置与本地/外部模型连通性
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import (AgentConfig, check_connectivity, load_config,
                     masked_config)
from .ingest.hls import DownloadError
from .improve import export_sft, run_improvement
from .ingest.loader import (AUDIO_EXTS, IngestError, VIDEO_EXTS,
                            classify_url, load_source, resolve_media_url)
from .ingest.transcriber import TranscribeError, segments_to_srt, transcribe_media
from .llm import LLMError, MockLLM, OpenAICompatClient, Router
from .memory import Memory
from .outputs import write_all
from .pipeline.executor import ExecuteError
from .pipeline.planner import PlanError
from .pipeline.orchestrator import Orchestrator
from .prompts import PromptError
from .utils import safe_read_text, setup_logging

_log = logging.getLogger("course_agent")


def build_llm(cfg: AgentConfig, mock: bool = False, source: str = "",
              model_override: str = ""):
    if mock:
        return MockLLM()
    local = OpenAICompatClient(
        base_url=cfg.local.base_url,
        model=model_override or cfg.local.model,
        api_key=cfg.local.api_key,
        timeout=cfg.local.timeout,
        max_retries=cfg.local.max_retries,
    )
    api = None
    if cfg.api.enabled and cfg.api_key_effective:
        api = OpenAICompatClient(
            base_url=cfg.api.base_url, model=cfg.api.model,
            api_key=cfg.api_key_effective, timeout=cfg.api.timeout,
            max_retries=cfg.api.max_retries)
    clients = [local] + ([api] if api else [])
    prefer = source or cfg.prefer
    if prefer == "api" and api is None:
        raise LLMError("指定了 --source api，但外部 API 未启用或未配置密钥")
    return Router(clients, prefer="api" if prefer == "api" else "local")


def _open_memory(cfg: AgentConfig, enabled: bool = True) -> Optional[Memory]:
    if not enabled:
        return None
    db = Path(cfg.data_dir) / "memory.db"
    mem = Memory(db)
    _log.debug("记忆库: %s", db)
    return mem


def _resolve_review_llm(cfg: AgentConfig, llm, mock: bool):
    if mock or cfg.review_source != "api":
        return None
    if not (cfg.api.enabled and cfg.api_key_effective):
        _log.warning("review_source=api 但外部 API 未启用，改用同一模型审查")
        return None
    return OpenAICompatClient(
        base_url=cfg.api.base_url, model=cfg.api.model,
        api_key=cfg.api_key_effective, timeout=cfg.api.timeout,
        max_retries=cfg.api.max_retries)


# ------------------------------------------------------------------ commands
def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.max_attempts:
        cfg.pipeline.max_attempts = args.max_attempts
    if args.chunk_chars:
        cfg.pipeline.chunk_chars = args.chunk_chars
    setup_logging(args.verbose)
    llm = build_llm(cfg, mock=args.mock, source=args.source, model_override=args.model)
    review_llm = _resolve_review_llm(cfg, llm, args.mock)
    material = load_source(args.input, password=args.password or "",
                           workdir=cfg.data_dir)
    title = args.title or material.title
    mem = _open_memory(cfg, enabled=not args.no_memory)
    try:
        orch = Orchestrator(llm, cfg, review_llm=review_llm, memory=mem)
        result = orch.run(material, title=title)
        outdir = args.outdir or str(Path(cfg.output_dir) / result.session_id)
        files = write_all(result, outdir)
        result.output_dir = outdir
    finally:
        if mem is not None:
            mem.close()
    _log.info("完成：%s（得分 %s）", "通过质检" if result.passed else "未通过质检",
              result.qa.score if result.qa else "-")
    for name in ("notes.md", "key_nodes.md", "exercises.md", "review_report.md"):
        print(f"  {files.get(name, name)}")
    return 0 if result.passed else 2


def cmd_demo(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    example = Path(__file__).resolve().parent.parent / "examples" / "sample_lecture.srt"
    if not example.is_file():
        _log.error("示例文件缺失: %s", example)
        return 1
    material = load_source(str(example))
    orch = Orchestrator(MockLLM(), cfg, memory=None)
    result = orch.run(material, title="示例直播课：Spring Boot 事务与并发")
    outdir = args.outdir or str(Path(cfg.output_dir) / "demo")
    files = write_all(result, outdir)
    _log.info("离线演示完成（Mock 模型，不调用任何网络）")
    for name in ("notes.md", "key_nodes.md", "exercises.md", "review_report.md"):
        print(f"  {files.get(name, name)}")
    return 0 if result.passed else 1


def cmd_improve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(args.verbose)
    llm = build_llm(cfg, mock=args.mock, source=args.source)
    review_llm = _resolve_review_llm(cfg, llm, args.mock)
    inputs = []
    for i, src in enumerate(args.input):
        title = args.title[i] if args.title and i < len(args.title) else Path(src).stem
        inputs.append((src, title))
    with _open_memory(cfg) as mem:
        assert mem is not None
        summary = run_improvement(llm, cfg, mem, inputs,
                                  rounds=args.rounds, review_llm=review_llm)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_rate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(args.verbose)
    if not (1 <= args.score <= 5):
        _log.error("score 取值 1~5")
        return 1
    with _open_memory(cfg) as mem:
        assert mem is not None
        mem.add_feedback(args.session, args.section, args.score, args.note or "")
    _log.info("反馈已记录：会话 %s 章节「%s」评分 %d", args.session, args.section, args.score)
    return 0


def cmd_export_sft(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(args.verbose)
    with _open_memory(cfg) as mem:
        assert mem is not None
        n = export_sft(mem, args.out)
    print(f"已导出 {n} 条样本: {args.out}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """只做 下载回放 -> 本地转写（srt/txt），不跑整理管线。便于先验证素材可用。"""
    import re as _re
    cfg = load_config(args.config)
    setup_logging(args.verbose)
    src = args.input
    if _re.match(r"^https?://", src):
        if classify_url(src) not in ("tencent_share", "media"):
            raise IngestError("fetch 仅支持 腾讯会议回放链接 / m3u8、mp4 媒体链接；普通网页请直接用 run")
        local = resolve_media_url(src, password=args.password or "",
                                  workdir=cfg.data_dir)
    else:
        p = Path(src)
        if not (p.is_file() and p.suffix.lower() in (AUDIO_EXTS | VIDEO_EXTS)):
            raise IngestError("fetch 仅用于 腾讯会议回放链接 / 视频(m3u8/mp4)链接 / 本地音视频文件")
        local = p
    segments = transcribe_media(local)
    title = args.title or local.stem
    outdir = Path(args.outdir or (Path(cfg.data_dir) / "transcripts"))
    outdir.mkdir(parents=True, exist_ok=True)
    srt_path = outdir / f"{title}.srt"
    txt_path = outdir / f"{title}.txt"
    srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
    txt_path.write_text("\n".join(seg.text for seg in segments), encoding="utf-8")
    print(f"  {srt_path}")
    print(f"  {txt_path}")
    _log.info("接下来可运行: python main.py run --input \"%s\" --title \"%s\"", srt_path, title)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    report = check_connectivity(cfg)
    print(json.dumps({"config": masked_config(cfg), "connectivity": report},
                     ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="course-agent",
        description="直播课程知识整理助手：知识点/关键节点/习题自动生成")
    p.add_argument("--version", action="version", version=f"course-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, config: bool = True) -> None:
        if config:
            sp.add_argument("--config", default=None, help="config.json 路径")
        sp.add_argument("-v", "--verbose", action="store_true", help="调试日志")

    run = sub.add_parser("run", help="处理课程材料")
    run.add_argument("--input", required=True, help="素材路径或 http(s) URL")
    run.add_argument("--password", default="", help="课程页面密码（可选）")
    run.add_argument("--title", default="", help="课程标题（默认取文件名/网页标题）")
    run.add_argument("--mock", action="store_true", help="使用离线 Mock 模型")
    run.add_argument("--source", choices=["auto", "local", "api"], default="auto")
    run.add_argument("--model", default="", help="覆盖本地模型名")
    run.add_argument("--outdir", default="", help="输出目录（默认 output/<会话ID>）")
    run.add_argument("--max-attempts", type=int, default=0, help="覆盖外层重试上限")
    run.add_argument("--chunk-chars", type=int, default=0, help="覆盖分块大小")
    run.add_argument("--no-memory", action="store_true", help="不读写记忆库")
    common(run)

    demo = sub.add_parser("demo", help="离线演示")
    demo.add_argument("--outdir", default="", help="输出目录")
    common(demo)

    imp = sub.add_parser("improve", help="自改进循环")
    imp.add_argument("--input", nargs="+", required=True, help="样例素材列表")
    imp.add_argument("--title", nargs="*", default=None, help="与素材对应的标题")
    imp.add_argument("--rounds", type=int, default=2)
    imp.add_argument("--mock", action="store_true")
    imp.add_argument("--source", choices=["auto", "local", "api"], default="auto")
    common(imp)

    rate = sub.add_parser("rate", help="对会话章节打分")
    rate.add_argument("--session", required=True)
    rate.add_argument("--section", default="")
    rate.add_argument("--score", type=int, required=True)
    rate.add_argument("--note", default="")
    common(rate)

    exp = sub.add_parser("export-sft", help="导出微调样本")
    exp.add_argument("--out", default="data/sft.jsonl")
    common(exp)

    fetch = sub.add_parser("fetch", help="下载回放并转写为字幕/文稿（不整理）")
    fetch.add_argument("--input", required=True, help="腾讯会议回放链接 / m3u8/mp4 URL / 本地音视频")
    fetch.add_argument("--password", default="", help="提取码")
    fetch.add_argument("--title", default="", help="输出文件名（默认取链接/文件名）")
    fetch.add_argument("--outdir", default="", help="输出目录（默认 data/transcripts）")
    common(fetch)

    conf = sub.add_parser("config", help="查看配置与连通性")
    common(conf)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"run": cmd_run, "demo": cmd_demo, "improve": cmd_improve,
                "rate": cmd_rate, "export-sft": cmd_export_sft,
                "fetch": cmd_fetch, "config": cmd_config}
    try:
        return handlers[args.command](args)
    except (LLMError, ValueError, FileNotFoundError, IngestError,
            TranscribeError, PlanError, ExecuteError, PromptError,
            DownloadError) as e:
        _log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        _log.warning("已中断")
        return 130
