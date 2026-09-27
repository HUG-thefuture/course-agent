"""路由器：本地模型优先，失败自动回落外部 API；也可指定只用其一。"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .base import BaseLLM, LLMError

_log = logging.getLogger(__name__)


class Router(BaseLLM):
    """把多个客户端按优先级串联。线程安全（served_by 加锁更新）。"""

    def __init__(self, clients: list[BaseLLM], prefer: str = "local") -> None:
        if not clients:
            raise LLMError("Router 需要至少一个模型客户端")
        self._clients = list(clients)
        self.prefer = prefer
        self._served_by: str = ""
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

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2048) -> str:
        errors: list[str] = []
        for client in self._order():
            try:
                out = client.chat(system, user,
                                  temperature=temperature, max_tokens=max_tokens)
                with self._lock:
                    self._served_by = client.name
                return out
            except LLMError as e:
                errors.append(f"[{client.name}] {e}")
                _log.warning("模型 %s 调用失败，尝试下一个: %s", client.name, e)
        raise LLMError("所有模型均调用失败:\n" + "\n".join(errors))
