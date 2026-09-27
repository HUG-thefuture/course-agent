"""配置加载：config.json + 环境变量覆盖，全部带安全默认值。"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen3:8b"
DEFAULT_API_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_API_MODEL = "deepseek-chat"
ENV_API_KEY = "COURSE_AGENT_API_KEY"


@dataclass
class LocalConfig:
    base_url: str = DEFAULT_LOCAL_BASE_URL
    model: str = DEFAULT_LOCAL_MODEL
    api_key: str = ""
    timeout: int = 300
    max_retries: int = 2


@dataclass
class ApiConfig:
    enabled: bool = False
    base_url: str = DEFAULT_API_BASE_URL
    model: str = DEFAULT_API_MODEL
    api_key: str = ""
    timeout: int = 120
    max_retries: int = 2


@dataclass
class PipelineConfig:
    chunk_chars: int = 3000
    max_attempts: int = 3          # 思考->计划->审查->执行->测试 外层循环上限，保证必然终止
    min_exercises: int = 3
    max_exercises: int = 6
    fidelity_sample: int = 2       # 测试阶段每节抽查的条目数
    fidelity_threshold: float = 0.8
    exemplar_min_score: int = 70   # 达到此分数的章节才沉淀为少样本范例


@dataclass
class AgentConfig:
    local: LocalConfig = field(default_factory=LocalConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    prefer: str = "local"          # local / api / auto(local 优先，失败回落 api)
    review_source: str = "same"    # same / api（用外部模型独立审查，更客观）
    data_dir: str = "data"
    output_dir: str = "output"

    @property
    def api_key_effective(self) -> str:
        return self.api.api_key or os.environ.get(ENV_API_KEY, "")


def _merge(dc_instance: Any, patch: dict[str, Any]) -> Any:
    """把字典浅合并进 dataclass 实例，忽略未知键。"""
    if not isinstance(patch, dict):
        return dc_instance
    for k, v in patch.items():
        if hasattr(dc_instance, k) and not k.startswith("_"):
            setattr(dc_instance, k, v)
    return dc_instance


def load_config(path: str | Path | None = None) -> AgentConfig:
    """从 JSON 文件加载配置；缺省时依次找 ./config.json，否则用默认值。"""
    cfg = AgentConfig()
    candidates = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("COURSE_AGENT_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("config.json"))
    for p in candidates:
        if p and Path(p).is_file():
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            _merge(cfg.local, data.get("local", {}))
            _merge(cfg.api, data.get("api", {}))
            _merge(cfg.pipeline, data.get("pipeline", {}))
            for k in ("prefer", "review_source", "data_dir", "output_dir"):
                if k in data:
                    setattr(cfg, k, data[k])
            break
    return cfg


def masked_config(cfg: AgentConfig) -> dict[str, Any]:
    d = asdict(cfg)
    key = cfg.api_key_effective
    d["api"]["api_key"] = (key[:4] + "***") if key else ""
    return d


def _ping(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """探测 OpenAI 兼容服务是否可达（GET /models）。"""
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return True, body[:500]
    except Exception as e:  # 网络/超时/HTTP 错误都归为不可达
        return False, str(e)


def check_connectivity(cfg: AgentConfig) -> dict[str, Any]:
    """检查本地与外部 API 可达性，返回报告字典。"""
    ok_local, detail_local = _ping(cfg.local.base_url)
    report: dict[str, Any] = {
        "local": {"reachable": ok_local, "base_url": cfg.local.base_url, "model": cfg.local.model},
        "api": {"enabled": cfg.api.enabled},
    }
    if cfg.api.enabled:
        ok_api, detail_api = _ping(cfg.api.base_url)
        report["api"]["reachable"] = ok_api
        report["api"]["base_url"] = cfg.api.base_url
        report["api"]["model"] = cfg.api.model
        if not ok_api:
            report["api"]["detail"] = detail_api
    else:
        report["api"]["reachable"] = None
        report["api"]["hint"] = "未启用外部 API；如需启用请在 config.json 设置 api.enabled=true 并配置密钥"
    return report
