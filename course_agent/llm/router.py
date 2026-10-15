"""路由器：本地模型优先，失败自动回落外部 API；也可指定只用其一。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .base import BaseLLM, LLMError

_log = logging.getLogger(__name__)

# 同一模型连续失败达到该次数后，在本会话后续调用中被跳过（直到它再成功一次自动复位）。
# 目的：某个模型卡死/超时时，不反复为它支付整段超时，避免整个管线拖死；
# 间歇性抖动（偶尔 502 但多数正常）不会连续攒够阈值，故不会被误跳过。
DEFAULT_SKIP_THRESHOLD = 3


class Router(BaseLLM):
    """把多个客户端按优先级串联。线程安全（served_by 加锁更新）。"""

    def __init__(self, clients: list[BaseLLM], prefer: str = "local",
                 skip_threshold: int = DEFAULT_SKIP_THRESHOLD) -> None:
        if not clients:
            raise LLMError("Router 需要至少一个模型客户端")
        self._clients = list(clients)
        self.prefer = prefer
        self.skip_threshold = max(0, int(skip_threshold))
        self._served_by: str = ""
        self._fails: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "router(" + ",".join(c.name for c in self._clients) + ")"

    @property
    def served_by(self) -> str:
        with self._lock:
            return self._served_by

    def _order(self) -> list[BaseLLM]:
        if self.prefer == "api" and len(self._clients) > 1:
            return [self._clients[1], self._clients[0]]
        return list(self._clients)

    def _cooling_down(self) -> set[str]:
        """连续失败已达阈值、本次调用应跳过的模型名集合。"""
        with self._lock:
            return {n for n, c in self._fails.items() if c >= self.skip_threshold}

    def _bump_fail(self, client: BaseLLM) -> None:
        with self._lock:
            self._fails[client.name] = self._fails.get(client.name, 0) + 1

    def _reset_fail(self, client: BaseLLM) -> None:
        with self._lock:
            self._fails[client.name] = 0

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2048, **kwargs) -> str:
        errors: list[str] = []
        skip = self._cooling_down()
        candidates = [c for c in self._order() if c.name not in skip]
        # 全部都在冷却中：做一次完整回扫，保证总有候选（同时报告所有错误）
        if not candidates:
            candidates = list(self._order())
        for client in candidates:
            try:
                out = client.chat(system, user,
                                  temperature=temperature, max_tokens=max_tokens)
                self._reset_fail(client)
                with self._lock:
                    self._served_by = client.name
                return out
            except LLMError as e:
                errors.append(f"[{client.name}] {e}")
                self._bump_fail(client)
                _log.warning("模型 %s 调用失败（连续 %d 次），尝试下一个: %s",
                             client.name, self._fails.get(client.name, 0), e)
        raise LLMError("所有模型均调用失败:\n" + "\n".join(errors))


class TieredRouter(BaseLLM):
    """按任务难度路由到不同模型档位（perf=高性能 / flash=快省，可自定义）。

    每个档位内部是一个独立 Router（故障转移 + 健康跳过），所以某个档位整体
    不可用时不影响其它档位。用法：
        llm.chat(system, user, task="notes")   # 自动映射到 notes 任务的档位
        llm.tier("perf")                       # 取某档位的子路由（独立调用）
    某档位内全部模型失败时，依次尝试其它档位，最后回落本地模型。
    """

    def __init__(self, cloud_clients: list[BaseLLM],
                 local_client: Optional[BaseLLM] = None,
                 tier_map: Optional[dict[str, str]] = None,
                 task_map: Optional[dict[str, str]] = None,
                 default_tier: str = "flash",
                 skip_threshold: int = DEFAULT_SKIP_THRESHOLD,
                 prefer: str = "api",
                 backoff_delay: float = 5.0) -> None:
        if not cloud_clients and local_client is None:
            raise LLMError("TieredRouter 需要至少一个模型客户端")
        self.default_tier = default_tier
        self._task_map = task_map or {}
        self._local = local_client
        self._prefer_local = (prefer == "local")
        self._backoff_delay = backoff_delay
        by_tier: dict[str, list[BaseLLM]] = {}
        for c in cloud_clients:
            t = (tier_map or {}).get(getattr(c, "model", ""), default_tier)
            by_tier.setdefault(t, []).append(c)
        # 档位尝试顺序：perf 优先，其余按出现顺序
        keys = list(by_tier.keys())
        self._tier_order: list[str] = (
            ["perf"] + [k for k in keys if k != "perf"] if "perf" in keys else keys)
        # 每个档位的子路由：云端模型已按「服务商 -> 模型」顺序排列，prefer=local 即保持原序
        self._routers: dict[str, Router] = {
            t: Router(cs, skip_threshold=skip_threshold)
            for t, cs in by_tier.items()
        }
        self._log = logging.getLogger(__name__)

    @property
    def name(self) -> str:
        return "tiered-router(" + "/".join(self._tier_order) + ")"

    @property
    def served_by(self) -> str:
        for t in self._tier_order:
            r = self._routers.get(t)
            if r and r.served_by:
                return f"[{t}] {r.served_by}"
        if self._local is not None:
            return f"[local] {self._local.name}"
        return ""

    def tier(self, name: str = "") -> BaseLLM:
        """取某档位的子路由；未知档位回落到 default 档。"""
        return (self._routers.get(name or self.default_tier)
                or self._routers.get(self.default_tier)
                or next(iter(self._routers.values()))
                or self._local)

    def _resolve_tier(self, kwargs: dict) -> str:
        tier = kwargs.get("tier") or ""
        if tier:
            return tier
        task = kwargs.get("task") or ""
        return self._task_map.get(task) or self.default_tier

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2048, **kwargs) -> str:
        tier = self._resolve_tier(kwargs)
        # 目标档 -> 其它档 -> 本地，依次尝试；子路由内部已做档内故障转移
        seq: list[BaseLLM] = []
        if self._prefer_local and self._local is not None:
            seq.append(self._local)
        if tier in self._routers:
            seq.append(self._routers[tier])
        for t in self._tier_order:
            if t != tier and self._routers.get(t) is not None:
                seq.append(self._routers[t])
        if not self._prefer_local and self._local is not None:
            seq.append(self._local)
        errors: list[str] = []
        for i, sub in enumerate(seq):
            try:
                return sub.chat(system, user, temperature=temperature,
                                max_tokens=max_tokens)
            except Exception as e:  # 跨档兜底：子路由任何异常都尝试下一档
                errors.append(str(e))
                # 跨档退避：给限流窗口恢复时间
                if i < len(seq) - 1 and self._backoff_delay > 0:
                    self._log.warning("档位 %s 失败，%.0fs 后尝试下一档",
                                      getattr(sub, "name", "?"),
                                      self._backoff_delay)
                    time.sleep(self._backoff_delay)
        # 全部档位失败：退避后重试目标档一次（限流窗口可能已恢复）
        if self._backoff_delay > 0 and tier in self._routers:
            wait = self._backoff_delay * 2
            self._log.warning("所有档位失败，%.0fs 后重试目标档 %s", wait, tier)
            time.sleep(wait)
            try:
                return self._routers[tier].chat(system, user,
                                                temperature=temperature,
                                                max_tokens=max_tokens)
            except Exception as e:
                errors.append(f"[retry {tier}] {e}")
        raise LLMError("所有档位/模型均调用失败:\n" + "\n".join(errors))
