"""模型健康探测：并发试调每个已配置模型，记录可用性与延迟。

为什么需要它：DeepSeek Harness 的模型清单里混着已下线（HTTP 410）、
限流（429）、超时（读超时）的条目。直接把全量模型塞进故障转移链会让每次
失败都要等一个完整超时，反而拖慢管线。先探活，再让配置只保留真正能用的。

用法：
  python tools/probe_models.py                 # 探测并写 data/model_health.json
  python tools/probe_models.py --timeout 25 --workers 16
  python tools/probe_models.py --apply         # 探测后把不可用模型从 config.json 剔除
  python tools/probe_models.py --report        # 只读上次结果并打印汇总

结果文件（可断点续用）：
  data/model_health.json = { "<provider>::<model>": {"ok":bool,"ms":int,"tag":str,"detail":str} }
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import load_config  # noqa: E402
from course_agent.llm.factory import build_client_chains  # noqa: E402

HEALTH = ROOT / "data" / "model_health.json"
CONFIG = ROOT / "config.json"


def tag_of(msg: str) -> str:
    low = msg.lower()
    for code, tag in (("410", "EOL"), ("404", "404"), ("429", "429"),
                      ("401", "AUTH"), ("403", "AUTH")):
        if code in msg:
            return tag
    if "timed out" in low or "timeout" in low:
        return "TIMEOUT"
    if "empty" in msg or "空内容" in msg:
        return "EMPTY"
    return "ERR"


def is_vision_model(model_id: str) -> bool:
    """视觉/多模态模型：能对话，但纯文本任务上比纯文本模型慢且不稳，排后面。"""
    low = model_id.lower()
    return any(p in low for p in ("-vl", "vision", "v-4.7", "omni", "glimmer"))


def load_health() -> dict:
    try:
        return json.loads(HEALTH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_health(data: dict) -> None:
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def probe(client, timeout: float) -> tuple[bool, int, str, str]:
    """单模型一次最小补全。timeout 同时约束客户端内部超时。"""
    client.timeout = max(5, int(timeout))
    t0 = time.time()
    try:
        out = client.chat("只回复两个字。", "收到", max_tokens=8)
        return True, int((time.time() - t0) * 1000), "OK", (out or "").strip()[:24]
    except Exception as e:
        msg = str(e)
        return False, int((time.time() - t0) * 1000), tag_of(msg), msg[:180]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="模型健康探测")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--apply", action="store_true",
                    help="把探测失败的模型从 config.json 的 providers 中剔除")
    ap.add_argument("--report", action="store_true", help="只打印上次结果汇总")
    ap.add_argument("--from-report", action="store_true",
                    help="不重新探测，直接用 data/model_health.json 里上次的结果做 --apply")
    ap.add_argument("--limit", type=int, default=0, help="只探测前 N 个（调试）")
    args = ap.parse_args(argv[1:])

    if args.report:
        rows = load_health()
        print_report(rows)
        return 0

    if args.from_report:
        rows = load_health()
        if not rows:
            print("没有历史探测结果，先跑一次不带 --from-report 的探测")
            return 1
        print_report(rows)
        apply_to_config(rows)
        return 0

    cfg = load_config()
    _, cloud = build_client_chains(cfg)
    if args.limit:
        cloud = cloud[: args.limit]
    print(f"待探测 {len(cloud)} 个模型（timeout={args.timeout}s，并发 {args.workers}）…",
          flush=True)

    health = load_health()
    done = 0
    t_start = time.time()

    def work(c):
        host = c.base_url.split("//")[-1].split("/")[0]
        ok, ms, tag, detail = probe(c, args.timeout)
        return f"{host}::{c.model}", {"ok": ok, "ms": ms, "tag": tag, "detail": detail,
                                      "provider_host": host, "model": c.model}

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(work, c): c for c in cloud}
        for fut in cf.as_completed(futures):
            key, rec = fut.result()
            health[key] = rec
            done += 1
            mark = "✓" if rec["ok"] else "✗"
            print(f"[{done}/{len(cloud)}] {mark} {rec['provider_host'][:28]:<28} "
                  f"{rec['model'][:44]:<44} {rec['tag']:<8} {rec['ms']}ms",
                  flush=True)
            if done % 10 == 0:
                save_health(health)     # 边探边存，中断也不白跑

    save_health(health)
    print(f"\n完成，用时 {(time.time() - t_start) / 60:.1f} 分钟；结果 -> {HEALTH}")
    print_report(health)

    if args.apply:
        apply_to_config(health)
    return 0


def print_report(health: dict) -> None:
    if not health:
        print("（暂无探测结果）")
        return
    from collections import Counter
    by_host: dict[str, Counter] = {}
    for rec in health.values():
        host = rec.get("provider_host", "?")
        by_host.setdefault(host, Counter())[rec.get("tag", "?")] += 1
    ok = sum(1 for r in health.values() if r.get("ok"))
    print(f"可用 {ok}/{len(health)}")
    for host, cnt in sorted(by_host.items()):
        print(f"  {host}: {dict(cnt)}")
    good = sorted((r for r in health.values() if r.get("ok")),
                  key=lambda r: r.get("ms", 1 << 30))
    if good:
        print("最快可用模型：")
        for r in good[:10]:
            print(f"  {r['ms']:>6}ms  {r['model']}")


def apply_to_config(health: dict) -> None:
    """按探测结果调整 config.json：

    - 判定"确定不可用"（EOL 已下线 / 404 端点无此模型 / AUTH 鉴权失败）→ 从链里剔除；
    - 429 限流、TIMEOUT 超时、EMPTY 空输出 → 保留（多为并发探测打出来的瞬时状态），
      但排到"已验证可用"的模型之后，作为后备；
    - 只保留能正常对话的模型：embedding / rerank / OCR / 翻译 / 内容安全 / 语音这类
      专用模型即使返回 200 也不能用于课程生成，一并剔除；
    - 同一服务商内按实测延迟从快到慢排序：链首是"确实最快能用"的模型，
      避免每次首次调用都先撞一个超时。
    """
    if not CONFIG.is_file():
        print("没有 config.json，跳过 --apply")
        return
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    dropped: dict[str, int] = {}
    for p in cfg.get("providers") or []:
        host = (p.get("base_url") or "").split("//")[-1].split("/")[0]
        good: list[tuple[int, str]] = []
        maybe: list[tuple[int, str]] = []
        keep: list[str] = []
        for m in p.get("models") or []:
            if is_special_purpose(m):
                dropped["专用模型(非对话)"] = dropped.get("专用模型(非对话)", 0) + 1
                continue
            rec = health.get(f"{host}::{m}")
            if rec is None:
                keep.append(m)                      # 没探测过的保持原序，避免误删
                continue
            tag = rec.get("tag", "?")
            ms = int(rec.get("ms") or 10 ** 9)
            if rec.get("ok"):
                good.append((ms, m))
            elif tag in ("EOL", "404", "AUTH"):
                dropped[tag] = dropped.get(tag, 0) + 1
            else:
                maybe.append((ms, m))               # 429/TIMEOUT/EMPTY：留作后备
        good.sort()
        maybe.sort()
        p["models"] = [m for _, m in good] + keep + [m for _, m in maybe]
    cfg["providers"] = [p for p in (cfg.get("providers") or []) if p.get("models")]

    # 档位列表也按「先纯文本、再看延迟」排序：
    # 同档里视觉模型列在前面时，纯文本任务会先撞上视觉模型（慢、且可能拒答纯文本），
    # 所以纯文本模型优先，视觉模型作后备。
    fastest: dict[str, int] = {}
    for key, rec in health.items():
        if not rec.get("ok"):
            continue
        m = key.split("::", 1)[-1]
        fastest[m] = min(fastest.get(m, 10 ** 9), int(rec.get("ms") or 10 ** 9))
    tiers = ((cfg.get("model_tiers") or {}).get("tiers") or {})
    alive = {m for p in cfg["providers"] for m in p.get("models") or []}
    for tier, models in tiers.items():
        tiers[tier] = sorted(
            (m for m in models if m in alive),
            key=lambda m: (is_vision_model(m), fastest.get(m, 10 ** 8)))
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    total = sum(len(p.get("models") or []) for p in cfg["providers"])
    print(f"剔除详情: {dropped or '无'}")
    print(f"保留 {len(cfg['providers'])} 个服务商 / {total} 个模型"
          f"（已验证可用按延迟排序在前，限流/超时的排后面作后备）")
    for tier, models in tiers.items():
        print(f"  {tier} 档首个模型: {models[0] if models else '(空)'}")


# 专用模型（非对话生成）：即使接口返回 200 也不能用于课程笔记/习题生成。
# 覆盖：向量/重排/奖励、OCR 与文档解析、语音翻译与合成、内容安全、图像描述。
_SPECIAL_PATTERNS = (
    "embed", "embedqa", "nvclip", "rerank", "reward", "detector",
    "nemotron-parse", "riva", "translate", "content-safety", "safety-guard",
    "nemoguard", "guard-", "deplot", "kosmos-2", "fuyu",
    "mineru", "omnivoice", "calibration", "cosmos", "neva", "vila",
    "diffusiongemma",  # 图像生成/扩散模型，不是对话模型
)


def is_special_purpose(model_id: str) -> bool:
    low = model_id.lower()
    return any(p in low for p in _SPECIAL_PATTERNS)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
