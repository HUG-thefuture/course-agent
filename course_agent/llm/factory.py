"""按配置构建模型客户端链：本地兜底 + 主服务商 + 多服务商全模型故障转移。"""

from __future__ import annotations

from ..config import ProviderConfig
from .base import BaseLLM
from .openai_compat import OpenAICompatClient


def _cloud_clients(base_url: str, models: list[str], api_key: str,
                   timeout: int, max_retries: int,
                   extra_body: dict | None) -> list[BaseLLM]:
    return [OpenAICompatClient(base_url=base_url, model=m, api_key=api_key,
                               timeout=timeout, max_retries=max_retries,
                               extra_body=extra_body)
            for m in models if m]


def build_client_chains(cfg) -> tuple[list[BaseLLM], list[BaseLLM]]:
    """返回 (本地客户端列表, 云端客户端列表)。

    云端链 = 主 api 块的 models + providers 列表中所有启用服务商的 models，
    Router 依序尝试：任一模型空输出/超时/报错即自动切换下一个。
    """
    local = OpenAICompatClient(
        base_url=cfg.local.base_url, model=cfg.local.model,
        api_key=cfg.local.api_key, timeout=cfg.local.timeout,
        max_retries=cfg.local.max_retries)
    cloud: list[BaseLLM] = []
    if cfg.api.enabled and cfg.api_key_effective:
        models = [m for m in (cfg.api.models or [cfg.api.model]) if m]
        cloud += _cloud_clients(cfg.api.base_url, models,
                                cfg.api_key_effective, cfg.api.timeout,
                                cfg.api.max_retries, cfg.api.extra_body)
    for p in cfg.providers or []:
        if not isinstance(p, ProviderConfig) or not p.enabled:
            continue
        if not p.base_url or not p.api_key or not p.models:
            continue
        cloud += _cloud_clients(p.base_url, p.models, p.api_key,
                                p.timeout, p.max_retries, p.extra_body)
    return [local], cloud
