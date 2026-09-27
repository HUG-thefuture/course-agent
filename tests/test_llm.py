"""LLM 层：Mock 路由、OpenAI 兼容客户端重试、Router 回落。"""

import io
import json
import unittest
import urllib.error

from course_agent.llm import LLMError, MockLLM, OpenAICompatClient, Router
from course_agent.llm.openai_compat import OpenAICompatClient as Client
from course_agent.prompts import render, task_marker


def _ok_transport(content="hello"):
    def t(req, timeout):
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        return 200, body
    return t


def _err_transport(code=500):
    def t(req, timeout):
        raise urllib.error.HTTPError("http://x", code, "boom", None, io.BytesIO(b"srv err"))
    return t


class TestMock(unittest.TestCase):
    def test_task_marker(self):
        p = render("plan", title="T", chunk_count=2, chunk_count_minus=1,
                   min_exercises=3, max_exercises=6,
                   chunk_previews="#0: abc", feedback="")
        self.assertEqual(task_marker(p), "PLAN")

    def test_placeholders_cleaned(self):
        p = render("review", plan_json="{}", chunk_previews="#0: x")
        self.assertNotIn("{{", p)

    def test_mock_plan_output_parseable(self):
        from course_agent.utils import extract_json
        m = MockLLM()
        p = render("plan", title="测试课", chunk_count=3, chunk_count_minus=2,
                   min_exercises=3, max_exercises=6,
                   chunk_previews="#0: 甲\n#1: 乙\n#2: 丙", feedback="")
        data = extract_json(m.chat(p, "x"))
        self.assertEqual(len(data["sections"]), 3)
        self.assertEqual(data["sections"][0]["chunk_indices"], [0])


class TestOpenAICompat(unittest.TestCase):
    def setUp(self):
        # 测试中跳过退避睡眠
        self._orig_sleep = Client._sleep_backoff
        Client._sleep_backoff = staticmethod(lambda attempt: None)

    def tearDown(self):
        Client._sleep_backoff = self._orig_sleep

    def test_success(self):
        c = OpenAICompatClient("http://localhost:9/v1", "m", transport=_ok_transport("你好"))
        self.assertEqual(c.chat("s", "u"), "你好")
        self.assertIn("m", c.name)

    def test_retry_on_5xx(self):
        calls = {"n": 0}

        def flaky(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("http://x", 503, "s", None, io.BytesIO(b""))
            return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        c = OpenAICompatClient("http://localhost:9/v1", "m", max_retries=2, transport=flaky)
        self.assertEqual(c.chat("s", "u"), "ok")
        self.assertEqual(calls["n"], 2)

    def test_no_retry_on_400(self):
        calls = {"n": 0}

        def bad(req, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError("http://x", 400, "bad", None, io.BytesIO(b""))

        c = OpenAICompatClient("http://localhost:9/v1", "m", max_retries=3, transport=bad)
        with self.assertRaises(LLMError):
            c.chat("s", "u")
        self.assertEqual(calls["n"], 1)

    def test_bad_response_shape(self):
        def weird(req, timeout):
            return 200, b'{"choices": []}'
        c = OpenAICompatClient("http://localhost:9/v1", "m", max_retries=0, transport=weird)
        with self.assertRaises(LLMError):
            c.chat("s", "u")

    def test_requires_base_url(self):
        with self.assertRaises(LLMError):
            OpenAICompatClient("", "m")


class _BrokenLLM:
    """恒失败的假客户端（测试 Router 回落）。"""

    name = "broken"

    def chat(self, *a, **k):
        raise LLMError("模拟本地模型宕机")


class TestRouter(unittest.TestCase):
    def test_fallback_to_api(self):
        api = OpenAICompatClient("http://localhost:9/v1", "api-model",
                                 transport=_ok_transport("api答复"))
        r = Router([_BrokenLLM(), api])
        self.assertEqual(r.chat("s", "u"), "api答复")
        self.assertEqual(r.served_by, api.name)

    def test_all_fail(self):
        class _Broken2(_BrokenLLM):
            name = "broken2"
        r = Router([_BrokenLLM(), _Broken2()])
        with self.assertRaises(LLMError) as ctx:
            r.chat("s", "u")
        self.assertIn("均调用失败", str(ctx.exception))

    def test_prefer_api(self):
        local = OpenAICompatClient("http://localhost:9/v1", "local-m",
                                   transport=_ok_transport("local"))
        api = OpenAICompatClient("http://localhost:9/v1", "api-m",
                                 transport=_ok_transport("api"))
        r = Router([local, api], prefer="api")
        self.assertEqual(r.chat("s", "u"), "api")

    def test_requires_clients(self):
        with self.assertRaises(LLMError):
            Router([])


if __name__ == "__main__":
    unittest.main()
