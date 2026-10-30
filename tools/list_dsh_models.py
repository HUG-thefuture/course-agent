"""列出 DeepSeek Harness 里配置的全部服务商与模型（供 course-agent 路由参考）。"""

import json
import os
import sys
from pathlib import Path

import yaml

p = Path(os.path.expanduser("~")) / ".dsh" / "settings.yaml.imported"
d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
provs = (d.get("llm-pi-ai") or {}).get("providers") or {}


def resolve_key(env_name: str) -> str:
    """从 dsh 凭据文件里取 key（只显示是否存在，不打印明文）。"""
    cred = Path(os.path.expanduser("~")) / ".dsh" / ".credentials.yaml"
    try:
        c = yaml.safe_load(cred.read_text(encoding="utf-8")) or {}
        refs = c.get("refs") or {}
        return refs.get(env_name, "") or ""
    except Exception:
        return ""


out = []
for name, cfg in provs.items():
    models = cfg.get("models") or []
    key = resolve_key(cfg.get("apiKeyEnv") or "")
    out.append({
        "provider": name,
        "baseURL": cfg.get("baseURL"),
        "api": cfg.get("api") or "openai-completions",
        "apiKeyEnv": cfg.get("apiKeyEnv"),
        "hasKey": bool(key),
        "modelCount": len(models),
        "models": [m.get("id") for m in models],
    })

print(json.dumps(out, ensure_ascii=False, indent=1))
print("\n默认模型:", json.dumps(d.get("agent-default-model"), ensure_ascii=False))
