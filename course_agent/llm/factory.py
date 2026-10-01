"""按配置构建模型客户端链：本地兜底 + 云端多模型自动切换。"""

from __future__ import annotations

from .base import BaseLLM
from .openai_compat import OpenAICompatClient


def build_client_chains(cfg) -> tuple[list[BaseLLM], list[BaseLLM]]:
    """返回 (本地客户端列表, 云端客户端列表)。

    云端列表按 config.api.models 顺序构建（每个模型一个客户端），
    Router 依序尝试：任一模型空输出/超时/报错即自动切换下一个。
    """
    local = OpenAICompatClient(
        base_url=cfg.local.base_url, model=cfg.local.model,
        api_key=cfg.local.api_key, timeout=cfg.local.timeout,
        max_retries=cfg.local.max_retries)
    cloud: list[BaseLLM] = []
    if cfg.api.enabled and cfg.api_key_effective:
        models = [m for m in (cfg.api.models or [cfg.api.model]) if m]
        for m in models:
            cloud.append(OpenAICompatClient(
                base_url=cfg.api.base_url, model=m,
                api_key=cfg.api_key_effective, timeout=cfg.api.timeout,
                max_retries=cfg.api.max_retries, extra_body=cfg.api.extra_body))
    return [local], cloud
