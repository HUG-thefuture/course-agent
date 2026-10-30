"""从 DSH 本地网关同步模型的回归测试。"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import sync_gateway_models as gw  # noqa: E402


class TestModelFiltering(unittest.TestCase):
    def test_skip_non_chat_models(self):
        for m in ("nvidia/nv-embedqa-mistral-7b", "openai/whisper-large",
                  "google/deplot", "x/nemotron-parse", "y/rerank-v1"):
            self.assertTrue(gw.is_skipped(m), m)

    def test_keep_chat_models(self):
        for m in ("workbuddy-cn/deepseek-v4.1-flash", "trae/glm-5.1",
                  "qodercn/dfmodel", "our-free-model/gpt-oss-20b"):
            self.assertFalse(gw.is_skipped(m), m)

    def test_classify_perf(self):
        for m in ("deepseek-ai/deepseek-v4-pro", "workbuddy/glm-5.3",
                  "nvidia/nemotron-3-ultra-550b-a55b", "trae/kimi-k3"):
            self.assertEqual(gw.classify(m), "perf", m)

    def test_classify_flash(self):
        for m in ("workbuddy-cn/glm-5.3-flash", "trae/doubao-2.0-mini",
                  "amd/MiniCPM5-2B"):
            self.assertEqual(gw.classify(m), "flash", m)

    def test_channel_prefix_ignored_in_classify(self):
        """渠道前缀本身不该影响档位判定。"""
        self.assertEqual(gw.classify("workbuddy-cn/glm-5.3"),
                         gw.classify("qodercn/glm-5.3"))

    def test_provider_of(self):
        self.assertEqual(gw.provider_of("trae/glm-5.1"), "trae")
        self.assertEqual(gw.provider_of("bare-model"), "gateway")


class TestWriteConfig(unittest.TestCase):
    def _prev(self):
        return {
            "providers": [
                {"name": "dsh-gateway", "models": ["old"]},
                {"name": "nvidia", "models": ["n1"]},
            ],
            "model_tiers": {"tiers": {"perf": ["old"], "flash": []},
                            "tasks": {"notes": "perf"}},
            "other_key": "keep-me",
        }

    def test_gateway_is_first_and_direct_providers_kept(self):
        health = {"workbuddy/glm-5.3": {"ok": True, "ms": 100}}
        out = gw.write_config("http://127.0.0.1:8326/v1", "k",
                              ["workbuddy/glm-5.3"], health, self._prev())
        names = [p["name"] for p in out["providers"]]
        self.assertEqual(names[0], "dsh-gateway")
        self.assertIn("nvidia", names)
        self.assertEqual(len([n for n in names if n == "dsh-gateway"]), 1)  # 不重复

    def test_unrelated_keys_preserved(self):
        out = gw.write_config("http://x/v1", "k", [], {}, self._prev())
        self.assertEqual(out["other_key"], "keep-me")

    def test_failed_models_excluded(self):
        health = {
            "a/glm-5.3": {"ok": True, "ms": 100},
            "b/glm-5.3": {"ok": False, "ms": 50},
        }
        out = gw.write_config("http://x/v1", "k", ["a/glm-5.3", "b/glm-5.3"],
                              health, self._prev())
        self.assertEqual(out["providers"][0]["models"], ["a/glm-5.3"])

    def test_ranked_by_latency(self):
        health = {
            "a/glm-5.3": {"ok": True, "ms": 900},
            "b/glm-5.3": {"ok": True, "ms": 100},
        }
        out = gw.write_config("http://x/v1", "k", ["a/glm-5.3", "b/glm-5.3"],
                              health, self._prev())
        self.assertEqual(out["providers"][0]["models"][0], "b/glm-5.3")

    def test_existing_task_map_not_overwritten(self):
        out = gw.write_config("http://x/v1", "k", [], {}, self._prev())
        self.assertEqual(out["model_tiers"]["tasks"]["notes"], "perf")
        self.assertIn("plan", out["model_tiers"]["tasks"])   # 缺失的补默认


class TestGatewayIO(unittest.TestCase):
    def test_gateway_key_from_dsh_home(self):
        with mock.patch.object(gw, "DSH_HOME", Path("/nonexistent")):
            self.assertEqual(gw.gateway_key(), "")

    def test_gateway_base_override(self):
        self.assertEqual(gw.gateway_base("http://127.0.0.1:9999/v1/"),
                         "http://127.0.0.1:9999/v1")
        self.assertEqual(gw.gateway_base(""), gw.DEFAULT_BASE)


if __name__ == "__main__":
    unittest.main()
