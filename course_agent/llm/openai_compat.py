"""OpenAI 兼容 Chat Completions 客户端。

同时覆盖：Ollama(/v1)、LM Studio(/v1)、vLLM、DeepSeek、通义、OpenAI 等兼容端点。
使用 urllib 每请求独立连接（用完即关），从根本上避免连接堆积类资源泄漏。
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from .base import BaseLLM, LLMError

_log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 可注入的传输函数，便于测试
Transport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


def _default_transport(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


class OpenAICompatClient(BaseLLM):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = 300,
        max_retries: int = 2,
        transport: Optional[Transport] = None,
    ) -> None:
        if not base_url:
            raise LLMError("base_url 不能为空")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._transport = transport or _default_transport

    @property
    def name(self) -> str:
        return f"openai-compat({self.model}@{self.base_url})"

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2048) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user or "请按要求输出。"},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        return self._request(payload)

    def chat_with_image(self, system: str, user: str, images: list,
                        temperature: float = 0.3, max_tokens: int = 2048) -> str:
        """多图输入（OpenAI Vision 兼容格式），images 为本地图片路径列表。"""
        import base64
        from pathlib import Path

        content: list[dict] = [{"type": "text", "text": user or "请描述这些图片。"}]
        for img in images:
            p = Path(img)
            if not p.is_file():
                raise LLMError(f"图片不存在: {p}")
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        return self._request(payload)

    def _request(self, payload: dict) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                status, raw = self._transport(req, self.timeout)
                data = json.loads(raw.decode("utf-8", errors="replace"))
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise LLMError(f"{self.name} 返回的 content 类型异常: {type(content)}")
                return content
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in _RETRYABLE_STATUS and attempt < self.max_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise LLMError(f"{self.name} HTTP {e.code}: {self._safe_body(e)}") from e
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    ConnectionError, json.JSONDecodeError, KeyError, IndexError,
                    OSError, ValueError) as e:
                # 网络/超时/响应格式问题：可重试
                last_err = e
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise LLMError(f"{self.name} 调用失败: {e}") from e
        raise LLMError(f"{self.name} 调用失败: {last_err}")

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        delay = min(8.0, 0.8 * (2 ** attempt)) + random.uniform(0, 0.3)
        _log.debug("请求失败，%.1fs 后重试", delay)
        time.sleep(delay)

    @staticmethod
    def _safe_body(e: urllib.error.HTTPError) -> str:
        try:
            return e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            return ""
