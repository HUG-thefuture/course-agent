"""LLM 抽象基类与统一异常。"""

from __future__ import annotations

import abc


class LLMError(Exception):
    """大模型调用失败的统一异常。"""


class BaseLLM(abc.ABC):
    """所有模型客户端的公共协议：单轮 chat，返回文本。"""

    @abc.abstractmethod
    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2048, **kwargs) -> str:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError
