"""LLM 子包：统一导出。"""

from .base import BaseLLM, LLMError
from .mock import MockLLM
from .openai_compat import OpenAICompatClient
from .router import Router
from .vision import captions_for_range, describe_frames

__all__ = ["BaseLLM", "LLMError", "MockLLM", "OpenAICompatClient", "Router",
           "captions_for_range", "describe_frames"]
