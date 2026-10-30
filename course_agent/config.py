"""配置加载：config.json + 环境变量覆盖，全部带安全默认值。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .net import open_direct

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
    extra_body: dict = field(default_factory=dict)  # 透传给请求体（如关闭思考模式）
    models: list = field(default_factory=list)      # 云端模型切换链（按序自动故障转移）


@dataclass
class VisionConfig:
    """视觉模型（可选）：读取视频屏幕帧，给编程题提供"屏幕上演示了什么"的证据。

    未启用时视频处理退化为 纯语音转写 + 帧截图存档。
    """
    enabled: bool = False
    base_url: str = DEFAULT_LOCAL_BASE_URL
    model: str = "qwen2.5vl:7b"
    api_key: str = ""
    timeout: int = 300
    max_retries: int = 1


@dataclass
class PipelineConfig:
    chunk_chars: int = 3000
    max_attempts: int = 3          # 思考->计划->审查->执行->测试 外层循环上限，保证必然终止
    min_exercises: int = 3
    max_exercises: int = 6
    fidelity_sample: int = 2       # 测试阶段每节抽查的条目数
    fidelity_threshold: float = 0.8
    exemplar_min_score: int = 70   # 达到此分数的章节才沉淀为少样本范例
    coding_practice: bool = True   # 是否生成编程实战题（失败不影响主流程）
    partial_chars: int = 3000      # 素材总字数低于该值按"不完整课程"处理
    extract_frames: bool = True    # 视频素材是否提取屏幕关键帧存档
    max_frames: int = 24           # 单课程最多提取帧数
    retry_until_pass: int = 2      # 批量首轮后对失败课程的额外重试轮数（持续迭代直至无问题）


@dataclass
class ProviderConfig:
    """额外云端服务商：models 按序自动故障转移。"""

    name: str = ""
    enabled: bool = False
    base_url: str = ""
    models: list = field(default_factory=list)
    api_key: str = ""
    timeout: int = 120
    max_retries: int = 2
    extra_body: dict = field(default_factory=dict)


@dataclass
class AgentConfig:
    local: LocalConfig = field(default_factory=LocalConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    providers: list = field(default_factory=list)  # 多服务商（ProviderConfig）
    prefer: str = "local"          # local / api / auto(local 优先，失败回落 api)
    review_source: str = "same"    # same / api（用外部模型独立审查，更客观）
    data_dir: str = "data"
    output_dir: str = "output"
    # 任务难度 -> 模型档位：perf(高性能) / flash(快省)。tiers 为 档位->模型名 列表，
    # tasks 为 任务名->档位 映射，default 为未列出任务/模型使用的档位。
    model_tiers: dict = field(default_factory=dict)

    @property
    def api_key_effective(self) -> str:
        return self.api.api_key or os.environ.get(ENV_API_KEY, "")

    @property
    def vision_key_effective(self) -> str:
        return self.vision.api_key or os.environ.get(ENV_API_KEY, "")

    def tier_map(self) -> dict[str, str]:
        """模型名 -> 档位（从 model_tiers.tiers 派生）。"""
        out: dict[str, str] = {}
        for tier, models in ((self.model_tiers or {}).get("tiers") or {}).items():
            for m in models or []:
                out[m] = tier
        return out

    def default_tier(self) -> str:
        return (self.model_tiers or {}).get("default") or "flash"

    def task_tier(self, task: str) -> str:
        """任务名 -> 档位；未列出时用 default。"""
        return ((self.model_tiers or {}).get("tasks") or {}).get(task) or self.default_tier()


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
            _merge(cfg.vision, data.get("vision", {}))
            _merge(cfg.pipeline, data.get("pipeline", {}))
            if isinstance(data.get("providers"), list):
                cfg.providers = [
                    ProviderConfig(**{k: v for k, v in row.items()
                                      if k in ProviderConfig.__dataclass_fields__})
                    for row in data["providers"] if isinstance(row, dict)]
            for k in ("prefer", "review_source", "data_dir", "output_dir", "model_tiers"):
                if k in data:
                    setattr(cfg, k, data[k])
            break
    return cfg


def _mask(key: str) -> str:
    return (key[:4] + "***") if key else ""


def masked_config(cfg: AgentConfig) -> dict[str, Any]:
    """输出可打印的配置：所有密钥字段一律脱敏。"""
    d = asdict(cfg)
    d["api"]["api_key"] = _mask(cfg.api_key_effective)
    d["vision"]["api_key"] = _mask(cfg.vision_key_effective)
    d["local"]["api_key"] = _mask(cfg.local.api_key)
    for i, p in enumerate(cfg.providers):
        d["providers"][i]["api_key"] = _mask(p.api_key)
    return d


def _ping(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """探测 OpenAI 兼容服务是否可达（GET /models）。

    401/403 说明服务在线但端点要求认证（/models 常需 Bearer），视为可达。
    """
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with open_direct(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return True, body[:500]
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return True, f"可达（端点需认证，HTTP {e.code}）"
        return False, f"HTTP {e.code}"
    except Exception as e:  # 网络/超时归为不可达
        return False, str(e)


def check_connectivity(cfg: AgentConfig) -> dict[str, Any]:
    """检查本地、主 API 与各服务商的可达性，返回报告字典。"""
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
        report["api"]["model_count"] = len(cfg.api.models or [cfg.api.model])
        if not ok_api:
            report["api"]["detail"] = detail_api
    else:
        report["api"]["reachable"] = None
        report["api"]["hint"] = "未启用外部 API；如需启用请在 config.json 设置 api.enabled=true 并配置密钥"
    if cfg.providers:
        rows = []
        for p in cfg.providers:
            if not (p.enabled and p.base_url and p.api_key):
                rows.append({"name": p.name, "enabled": False,
                             "models": len(p.models or [])})
                continue
            ok, detail = _ping(p.base_url, timeout=5.0)
            rows.append({"name": p.name, "enabled": True, "reachable": ok,
                         "base_url": p.base_url, "models": len(p.models or []),
                         **({} if ok else {"detail": detail})})
        report["providers"] = rows
        report["cloud_model_total"] = sum(len(p.models or []) for p in cfg.providers)
    return report
