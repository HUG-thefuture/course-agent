"""从 DSH 本地 OpenAI 兼容网关同步全部渠道模型到 course-agent。

背景：DeepSeek Harness 里装的 `dsh-our-free-model` 插件把十几个账号渠道
（CodeBuddy/WorkBuddy 腾讯、Qoder/QoderCN、TRAE、Cline、ZCode、buddy、
Copilot、NVIDIA、AMD、商汤、DeepSeek…）统一暴露成一个本地 OpenAI 兼容网关
（默认 `127.0.0.1:8326`）。对 course-agent 来说，它就是**一个 base_url + 一把 Key
背后的几百个模型**——比逐个服务商配 key 简单得多，而且凭据永远不出宿主进程。

本脚本：
  1. 读 `$DSH_HOME/openai-gateway/api-key` 与网关端口（可用 --base-url 覆盖）；
  2. `GET /v1/models` 拉全量模型清单；
  3. （可选）并发冒烟测试，记录可用性与延迟到 data/model_health.json；
  4. 把**可用**模型写进 config.json 的 providers（一个 provider 指向网关），
     并按延迟排好 perf / flash 两档，供 TieredRouter 分任务路由。

用法：
  python tools/sync_gateway_models.py                  # 拉清单 + 写配置（不逐个测）
  python tools/sync_gateway_models.py --probe          # 先探活再只写可用模型（推荐）
  python tools/sync_gateway_models.py --probe --apply  # 探活并剔除当前配置里的坏模型
  python tools/sync_gateway_models.py --dry-run        # 只看会写入什么
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.net import open_direct  # noqa: E402

CONFIG = ROOT / "config.json"
HEALTH = ROOT / "data" / "model_health.json"
DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))

DEFAULT_BASE = "http://127.0.0.1:8326/v1"
DEFAULT_TIMEOUT = 300
DEFAULT_RETRIES = 0

# 高性能档：大模型 / 强推理。命中即 perf，其余归 flash。
PERF_PATTERNS = [
    r"v4-pro", r"v4\.1-flash", r"ultra", r"-27b", r"120b", r"70b", r"90b",
    r"gpt-oss-120b", r"kimi-k?3", r"glm-5\.[0-9]+$", r"deepseek-v4", r"minimax-m3",
    r"step-5", r"nemotron-3-super", r"nemotron-3-ultra", r"opus", r"sonnet",
    r"gpt-5", r"gpt-6", r"mistral-large", r"hy4", r"qwen3-max", r"qwen3\.8-27b",
]

# 不用于课程生成的模型：向量/重排/OCR/语音/图像/安全/自动补全/图表理解
SKIP_PATTERNS = [
    r"embed", r"rerank", r"reward", r"detector", r"moderation", r"guard",
    r"whisper", r"tts", r"-audio", r"speech", r"ocr", r"parse", r"vision-only",
    r"diffusion", r"image", r"-img", r"dall", r"flux", r"stable-diffusion",
    r"codestral", r"codellama", r"starcoder", r"codegen", r"tabnine",
    r"nemotron-parse", r"riva-", r"translate", r"content-safety", r"safety",
    r"deplot", r"kosmos", r"fuyu", r"nvclip", r"neva", r"vila",
]


def gateway_base(cli_base: str = "") -> str:
    if cli_base:
        return cli_base.rstrip("/")
    return DEFAULT_BASE


def gateway_key() -> str:
    p = DSH_HOME / "openai-gateway" / "api-key"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def fetch_models(base: str, key: str, timeout: float = 20.0) -> list[str]:
    req = urllib.request.Request(base + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with open_direct(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [m["id"] for m in (data.get("data") or []) if m.get("id")]


def is_skipped(model_id: str) -> bool:
    low = model_id.lower()
    return any(re.search(p, low) for p in SKIP_PATTERNS)


def classify(model_id: str) -> str:
    low = model_id.split("/", 1)[-1].lower()   # 去掉渠道前缀再匹配
    for pat in PERF_PATTERNS:
        if re.search(pat, low):
            return "perf"
    return "flash"


def provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else "gateway"


def probe_one(base: str, key: str, model: str, timeout: float) -> dict:
    """对单个模型发一次最小补全，返回 {ok, ms, tag, detail}。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：收到"}],
        "max_tokens": 32,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with open_direct(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        d = json.loads(raw)
        content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not (content or "").strip():
            return {"ok": False, "ms": int((time.time() - t0) * 1000),
                    "tag": "EMPTY", "detail": "空内容"}
        return {"ok": True, "ms": int((time.time() - t0) * 1000),
                "tag": "OK", "detail": content.strip()[:20]}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            detail = ""
        tag = {401: "AUTH", 403: "AUTH", 404: "404", 429: "429",
               502: "UPSTREAM", 503: "UPSTREAM"}.get(e.code, f"HTTP{e.code}")
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "tag": tag, "detail": detail}
    except Exception as e:
        low = str(e).lower()
        tag = "TIMEOUT" if ("timed out" in low or "timeout" in low) else "ERR"
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "tag": tag, "detail": str(e)[:160]}


def load_health() -> dict:
    try:
        return json.loads(HEALTH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_health(data: dict) -> None:
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def write_config(base: str, key: str, models: list[str], health: dict,
                 prev: dict) -> dict:
    """把网关写成一个 provider，并按延迟/是否可用排好两档。"""
    alive = [m for m in models if health.get(m, {}).get("ok", True)]
    ranked = sorted(alive, key=lambda m: (
        not health.get(m, {}).get("ok", False),
        health.get(m, {}).get("ms", 10 ** 8)))

    perf = [m for m in ranked if classify(m) == "perf"]
    flash = [m for m in ranked if classify(m) != "perf"]

    tasks = ((prev.get("model_tiers") or {}).get("tasks") or {})
    tasks.setdefault("think", "flash")
    for k in ("plan", "review", "notes", "exercises", "coding", "qa"):
        tasks.setdefault(k, "perf")

    provider = {
        "name": "dsh-gateway",
        "enabled": True,
        "base_url": base,
        "models": ranked,
        "api_key": key,
        "timeout": DEFAULT_TIMEOUT,
        "max_retries": DEFAULT_RETRIES,
        "_说明": ("由 tools/sync_gateway_models.py 从 DeepSeek Harness 本地网关同步；"
                  "一个 base_url 代理十几个账号渠道，凭据不出宿主进程"),
    }

    new = dict(prev)
    # 保留既有非网关 provider（nvidia/amd/st-daily-use 等直连），把网关放到链首
    old_providers = [p for p in (prev.get("providers") or [])
                     if p.get("name") != "dsh-gateway"]
    new["providers"] = [provider] + old_providers
    new["model_tiers"] = {
        "default": (prev.get("model_tiers") or {}).get("default") or "flash",
        "tiers": {"perf": perf, "flash": flash},
        "tasks": tasks,
    }
    return new


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="从 DSH 本地网关同步全部渠道模型")
    ap.add_argument("--base-url", default="", help=f"网关地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--probe", action="store_true", help="并发冒烟测试，只写可用模型")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--probe-timeout", type=float, default=25.0)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv[1:])

    base = gateway_base(args.base_url)
    key = gateway_key()
    if not key:
        print(f"未找到网关 Key：{DSH_HOME / 'openai-gateway' / 'api-key'}")
        return 1

    print(f"网关: {base}")
    try:
        models = fetch_models(base, key)
    except Exception as e:
        print(f"拉取模型清单失败（网关是否在运行？）：{type(e).__name__} {e}")
        return 1
    print(f"网关报告 {len(models)} 个模型")

    usable = [m for m in models if not is_skipped(m)]
    skipped = [m for m in models if is_skipped(m)]
    if args.limit:
        usable = usable[: args.limit]
    print(f"剔除 {len(skipped)} 个非对话模型（向量/OCR/语音/图像/补全），"
          f"待处理 {len(usable)} 个")

    health = load_health()
    if args.probe:
        print(f"\n并发探活 {len(usable)} 个（timeout={args.probe_timeout}s，"
              f"并发 {args.workers}）…", flush=True)
        done = 0
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(probe_one, base, key, m, args.probe_timeout): m
                    for m in usable}
            for fut in cf.as_completed(futs):
                m = futs[fut]
                rec = fut.result()
                health[m] = rec
                done += 1
                mark = "✓" if rec["ok"] else "✗"
                print(f"[{done}/{len(usable)}] {mark} {m[:52]:<52} "
                      f"{rec['tag']:<9} {rec['ms']}ms", flush=True)
                if done % 20 == 0:
                    save_health(health)
        save_health(health)
        ok = sum(1 for m in usable if health.get(m, {}).get("ok"))
        print(f"\n探活完成，{ok}/{len(usable)} 可用，"
              f"用时 {(time.time() - t0) / 60:.1f} 分钟")

    prev = {}
    if CONFIG.is_file():
        try:
            prev = json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}

    new = write_config(base, key, usable, health, prev)
    perf = new["model_tiers"]["tiers"]["perf"]
    flash = new["model_tiers"]["tiers"]["flash"]
    total = len(new["providers"][0]["models"])

    print(f"\n将写入：{total} 个模型（perf {len(perf)} / flash {len(flash)}）")
    print(f"  perf 前 8: {perf[:8]}")
    print(f"  flash 前 8: {flash[:8]}")
    if len(new["providers"]) > 1:
        print(f"  另保留 {len(new['providers']) - 1} 个直连服务商："
              f"{[p.get('name') for p in new['providers'][1:]]}")

    if args.dry_run:
        print("\n--dry-run：未写文件")
        return 0

    if CONFIG.is_file():
        backup = CONFIG.with_suffix(".json.bak")
        backup.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"\n已备份 -> {backup.name}")
    CONFIG.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"已写入 {CONFIG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
