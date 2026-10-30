"""把 DeepSeek Harness 里已配置的全部服务商/模型同步进 course-agent 配置。

来源（只读，不改 DSH 任何文件）：
  ~/.dsh/settings.yaml.imported  -> llm-pi-ai.providers（baseURL / models / apiKeyEnv）
  ~/.dsh/.credentials.yaml       -> refs（apiKeyEnv -> 实际密钥）
  各服务商 GET /v1/models        -> 该 Key 当前真实可调用的全量模型（--discover）

产出：写入 course-agent 的 config.json
  - providers：每个服务商一条（含 base_url / api_key / models 全量模型）
  - model_tiers.tiers：按模型强弱分成 perf / flash 两档，供 TieredRouter 分任务路由
  - model_tiers.tasks：沿用已有任务映射（缺失时补默认）

合并策略：只改 providers 与 model_tiers 两个键，其余字段（local/api/pipeline/
inject/说明等）原样保留；写前备份 config.json。

用法：
  python tools/sync_dsh_models.py                    # 按 DSH 配置里的模型同步
  python tools/sync_dsh_models.py --discover         # 再加联网发现各端点全部可用模型
  python tools/sync_dsh_models.py --dry-run          # 只打印将写入的内容
  python tools/sync_dsh_models.py --discover --test  # 同步后逐个模型冒烟（慢）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

CONFIG = ROOT / "config.json"
DSH_HOME = Path(os.path.expanduser("~")) / ".dsh"
DSH_SETTINGS = DSH_HOME / "settings.yaml.imported"
DSH_CREDS = DSH_HOME / ".credentials.yaml"

# 性能档（大模型 / 强推理）：命中即算 perf，其余归 flash。
# 只影响「任务分档」，不影响可用性——两档都是完整故障转移链。
PERF_PATTERNS = [
    r"ultra", r"-27b", r"120b", r"70b", r"90b", r"gpt-oss-120b",
    r"kimi-k3", r"glm-5\.[23]$", r"glm-5\.2", r"deepseek-v4", r"minimax-m3",
    r"qwen3\.8-27b", r"step-3\.7", r"nemotron-3-super", r"nemotron-3-ultra",
    r"deepseek-v4-pro", r"mistral-small-4", r"kimi-k2\.6",
    r"mineru",
]

# 低优先级/不适合当主力生成的模型（仅保留可用，不放进默认档位链首位）
DEFAULT_TIMEOUT = 300
DEFAULT_RETRIES = 0


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def dsh_providers() -> list[dict]:
    """DSH 配置 -> [{name, base_url, api_key, models[], api}]（仅含有密钥的）。"""
    settings = _load_yaml(DSH_SETTINGS)
    creds = (_load_yaml(DSH_CREDS).get("refs") or {})
    provs = ((settings.get("llm-pi-ai") or {}).get("providers") or {})
    out = []
    for name, cfg in provs.items():
        env_name = cfg.get("apiKeyEnv") or ""
        key = creds.get(env_name) or os.environ.get(env_name, "")
        base_url = cfg.get("baseURL") or ""
        models = [m.get("id") for m in (cfg.get("models") or []) if m.get("id")]
        if not (base_url and key and models):
            continue
        out.append({"name": name, "base_url": base_url.rstrip("/"),
                    "api_key": key, "models": models,
                    "api": cfg.get("api") or "openai-completions"})
    return out


def discover_models(p: dict, timeout: float = 30.0) -> list[str]:
    """查端点 /v1/models，返回该 Key 当前真实可调用的模型 id（失败返回空）。"""
    import urllib.error
    import urllib.request

    from course_agent.net import open_direct
    req = urllib.request.Request(
        p["base_url"] + "/models",
        headers={"Authorization": f"Bearer {p['api_key']}"}, method="GET")
    try:
        body = open_direct(req, timeout=timeout).read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    rows = data.get("data") or data.get("models") or []
    out = []
    for m in rows:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            out.append(mid)
    return out


def classify(model_id: str) -> str:
    low = model_id.lower()
    for pat in PERF_PATTERNS:
        if re.search(pat, low):
            return "perf"
    return "flash"


def build_providers_block(provs: list[dict]) -> list[dict]:
    rows = []
    for p in provs:
        rows.append({
            "name": p["name"],
            "enabled": True,
            "base_url": p["base_url"],
            "models": p["models"],
            "api_key": p["api_key"],
            "timeout": DEFAULT_TIMEOUT,
            "max_retries": DEFAULT_RETRIES,
            "_说明": f"由 tools/sync_dsh_models.py 从 DeepSeek Harness 同步（{len(p['models'])} 个模型）",
        })
    return rows


def build_tiers_block(provs: list[dict], prev: dict) -> dict:
    perf: list[str] = []
    flash: list[str] = []
    for p in provs:
        for m in p["models"]:
            (perf if classify(m) == "perf" else flash).append(m)
    tasks = (prev.get("tasks") or {}) if isinstance(prev, dict) else {}
    tasks.setdefault("think", "flash")
    for k in ("plan", "review", "notes", "exercises", "coding", "qa"):
        tasks.setdefault(k, "perf")
    return {"default": (prev or {}).get("default") or "flash",
            "tiers": {"perf": perf, "flash": flash},
            "tasks": tasks}


def test_one(p: dict, model: str, timeout: float = 60.0) -> tuple[bool, str]:
    """对单个模型做一次最小补全，验证可用性。"""
    from course_agent.llm.openai_compat import OpenAICompatClient
    c = OpenAICompatClient(base_url=p["base_url"], model=model,
                           api_key=p["api_key"], timeout=int(timeout), max_retries=0)
    t0 = time.time()
    try:
        out = c.chat("你是测试助手，只回复两个字。", "回复：收到", max_tokens=16)
        return True, f"{time.time() - t0:.1f}s {out.strip()[:20]}"
    except Exception as e:
        return False, f"{time.time() - t0:.1f}s {str(e)[:120]}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="同步 DSH 模型到 course-agent 配置")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--discover", action="store_true",
                    help="联网查询各端点 /v1/models，把实际可用模型全部并入")
    ap.add_argument("--test", action="store_true", help="同步后逐模型冒烟测试")
    ap.add_argument("--only", default="", help="只处理/测试指定服务商名（逗号分隔）")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv[1:])

    provs = dsh_providers()
    if args.only:
        keep = {x.strip() for x in args.only.split(",") if x.strip()}
        provs = [p for p in provs if p["name"] in keep]
    if not provs:
        print("未从 DSH 配置中解析到任何可用服务商（检查 settings.yaml.imported 与 .credentials.yaml）")
        return 1

    if args.discover:
        print("联网发现各端点真实可用模型：")
        for p in provs:
            live = discover_models(p)
            if not live:
                print(f"  - {p['name']:<14} 查询失败，沿用 DSH 配置的 {len(p['models'])} 个")
                continue
            merged = list(dict.fromkeys(p["models"] + live))   # 配置里的在前，保持优先级
            print(f"  - {p['name']:<14} 配置 {len(p['models'])} + 端点 {len(live)} -> 合计 {len(merged)}")
            p["models"] = merged

    total = sum(len(p["models"]) for p in provs)
    print(f"从 DSH 解析到 {len(provs)} 个服务商，共 {total} 个模型：")
    for p in provs:
        print(f"  - {p['name']:<14} {len(p['models']):>2} 个  {p['base_url']}")

    old = {}
    if CONFIG.is_file():
        try:
            old = json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = {}
    new = dict(old)
    new["providers"] = build_providers_block(provs)
    new["model_tiers"] = build_tiers_block(provs, old.get("model_tiers") or {})
    tiers = new["model_tiers"]["tiers"]
    print(f"\n档位分布：perf {len(tiers['perf'])} 个 / flash {len(tiers['flash'])} 个")

    if args.dry_run:
        print(json.dumps({"providers": [
            {**p, "api_key": p["api_key"][:6] + "***"} for p in new["providers"]],
            "model_tiers": new["model_tiers"]}, ensure_ascii=False, indent=1))
        return 0

    if CONFIG.is_file():
        backup = CONFIG.with_suffix(".json.bak")
        backup.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"已备份旧配置 -> {backup.name}")
    CONFIG.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"已写入 {CONFIG}")

    if args.test:
        print("\n逐模型冒烟测试（每次一个最小请求）：")
        okn = 0
        fails: list[str] = []
        for p in provs:
            for m in p["models"]:
                ok, detail = test_one(p, m, timeout=args.timeout)
                mark = "✓" if ok else "✗"
                print(f"  [{mark}] {p['name']:<14} {m:<46} {detail}", flush=True)
                okn += 1 if ok else 0
                if not ok:
                    fails.append(f"{p['name']}/{m}: {detail}")
        print(f"\n可用 {okn}/{total}")
        if fails:
            print("不可用：")
            for f in fails:
                print("  -", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
