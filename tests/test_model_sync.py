"""DSH 模型同步 / 健康探测 / 配置连通性 的回归测试。"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import AgentConfig, ProviderConfig, check_connectivity  # noqa: E402
from tools import probe_models, sync_dsh_models  # noqa: E402


class TestSyncDshModels(unittest.TestCase):
    def test_classify_tiers(self):
        self.assertEqual(sync_dsh_models.classify("deepseek-v4-pro"), "perf")
        self.assertEqual(sync_dsh_models.classify("Qwen3.8-27B"), "perf")
        self.assertEqual(sync_dsh_models.classify("nvidia/nemotron-3-ultra-550b-a55b"), "perf")
        self.assertEqual(sync_dsh_models.classify("MiniCPM5-2B"), "flash")
        self.assertEqual(sync_dsh_models.classify("glm-5.3-flash"), "flash")

    def test_build_providers_block(self):
        rows = sync_dsh_models.build_providers_block([
            {"name": "p1", "base_url": "https://x/v1", "api_key": "k",
             "models": ["a", "b"], "api": "openai-completions"}])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["enabled"])
        self.assertEqual(rows[0]["models"], ["a", "b"])
        self.assertEqual(rows[0]["base_url"], "https://x/v1")

    def test_build_tiers_keeps_task_map(self):
        block = sync_dsh_models.build_tiers_block(
            [{"name": "p", "base_url": "u", "api_key": "k",
              "models": ["Qwen3.8-27B", "MiniCPM5-2B"], "api": "x"}],
            {"default": "perf", "tasks": {"notes": "flash"}})
        self.assertEqual(block["default"], "perf")
        self.assertEqual(block["tasks"]["notes"], "flash")   # 已有映射不被覆盖
        self.assertIn("plan", block["tasks"])                # 缺失的补默认
        self.assertEqual(block["tiers"]["perf"], ["Qwen3.8-27B"])
        self.assertEqual(block["tiers"]["flash"], ["MiniCPM5-2B"])

    def test_discover_models_parses_openai_shape(self):
        from unittest import mock

        class _Resp:
            def read(self):
                return json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(sync_dsh_models, "open_direct", create=True):
            pass  # 该函数内部惰性 import，直接打桩 course_agent.net.open_direct
        with mock.patch("course_agent.net.open_direct", return_value=_Resp()):
            got = sync_dsh_models.discover_models(
                {"base_url": "https://x/v1", "api_key": "k"})
        self.assertEqual(got, ["m1", "m2"])


class TestProbeModels(unittest.TestCase):
    def test_tag_of(self):
        self.assertEqual(probe_models.tag_of("HTTP 410 gone"), "EOL")
        self.assertEqual(probe_models.tag_of("HTTP 404"), "404")
        self.assertEqual(probe_models.tag_of("HTTP 429 slow down"), "429")
        self.assertEqual(probe_models.tag_of("read operation timed out"), "TIMEOUT")
        self.assertEqual(probe_models.tag_of("返回空内容"), "EMPTY")
        self.assertEqual(probe_models.tag_of("weird"), "ERR")

    def test_is_special_purpose(self):
        for m in ("nvidia/nv-embedqa-mistral-7b-v2", "nvidia/nemotron-parse",
                  "nvidia/riva-translate-4b-instruct", "nvidia/nemotron-3.5-content-safety"):
            self.assertTrue(probe_models.is_special_purpose(m), m)
        for m in ("DeepSeek-V4.1-Flash", "Qwen3.8-27B", "glm-5.2"):
            self.assertFalse(probe_models.is_special_purpose(m), m)

    def test_is_vision_model(self):
        self.assertTrue(probe_models.is_vision_model("nvidia/nemotron-3.5-super-vl-120b-a12b"))
        self.assertTrue(probe_models.is_vision_model("DeepSeek-V4-Flash-Vision-Exp"))
        self.assertFalse(probe_models.is_vision_model("DeepSeek-V4.1-Flash"))

    def test_apply_to_config_orders_and_prunes(self):
        import tempfile

        cfg = {
            "providers": [
                {"name": "p", "base_url": "https://h/v1",
                 "models": ["slow", "dead", "fast", "nvidia/nv-embedqa-m1"]},
            ],
            "model_tiers": {"tiers": {"perf": ["slow", "dead", "fast"],
                                      "flash": ["nvidia/nv-embedqa-m1"]}},
        }
        health = {
            "h::slow": {"ok": True, "ms": 900, "tag": "OK"},
            "h::dead": {"ok": False, "ms": 10, "tag": "EOL"},
            "h::fast": {"ok": True, "ms": 100, "tag": "OK"},
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            orig = probe_models.CONFIG
            probe_models.CONFIG = path
            try:
                probe_models.apply_to_config(health)
                out = json.loads(path.read_text(encoding="utf-8"))
            finally:
                probe_models.CONFIG = orig
        models = out["providers"][0]["models"]
        self.assertEqual(models, ["fast", "slow"])          # 死的剔除、按延迟排序
        self.assertEqual(out["model_tiers"]["tiers"]["perf"], ["fast", "slow"])
        self.assertEqual(out["model_tiers"]["tiers"]["flash"], [])  # 专用模型被清出档位


class TestConnectivityReport(unittest.TestCase):
    def test_providers_reported(self):
        from unittest import mock
        cfg = AgentConfig()
        cfg.providers = [
            ProviderConfig(name="good", enabled=True, base_url="https://g/v1",
                           api_key="k", models=["m1", "m2"]),
            ProviderConfig(name="off", enabled=False, base_url="https://o/v1",
                           api_key="k", models=["m3"]),
        ]
        with mock.patch("course_agent.config._ping", return_value=(True, "ok")):
            rep = check_connectivity(cfg)
        self.assertEqual(rep["cloud_model_total"], 3)
        names = {r["name"]: r for r in rep["providers"]}
        self.assertEqual(names["good"]["models"], 2)
        self.assertTrue(names["good"]["reachable"])
        self.assertFalse(names["off"]["enabled"])


if __name__ == "__main__":
    unittest.main()
