"""回归：密钥脱敏、BOM 读取、BM25 词频、Bloom 晋升。"""

import tempfile
import unittest
from pathlib import Path

from course_agent.config import AgentConfig, ProviderConfig, masked_config
from course_agent.kb import KnowledgeBase
from course_agent.review import BLOOM_LEVELS, ReviewStore, sm2_next
from course_agent.utils import safe_read_text


class TestMaskedConfig(unittest.TestCase):
    def test_all_keys_masked(self):
        cfg = AgentConfig()
        cfg.api.api_key = "sk-abcdef123456"
        cfg.vision.api_key = "vis-abcdef123456"
        cfg.local.api_key = "loc-abcdef123456"
        cfg.providers = [ProviderConfig(name="amd", api_key="amd-secret-key-99")]
        d = masked_config(cfg)
        self.assertTrue(d["api"]["api_key"].endswith("***"))
        self.assertTrue(d["vision"]["api_key"].endswith("***"))
        self.assertTrue(d["local"]["api_key"].endswith("***"))
        self.assertTrue(d["providers"][0]["api_key"].endswith("***"))
        blob = repr(d)
        self.assertNotIn("amd-secret-key-99", blob)
        self.assertNotIn("sk-abcdef123456", blob)


class TestSafeReadText(unittest.TestCase):
    def test_utf8_bom_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bom.json"
            p.write_bytes(b"\xef\xbb\xbf" + '[{"a": 1}]'.encode("utf-8"))
            text = safe_read_text(p)
            self.assertFalse(text.startswith("\ufeff"))
            import json
            self.assertEqual(json.loads(text), [{"a": 1}])


class TestKbTf(unittest.TestCase):
    def test_term_frequency_not_flattened(self):
        kb = KnowledgeBase(":memory:")
        # tokenize 中文按单字+相邻二元组：文本里 "向量" 二元组出现 3 次 vs 1 次
        chunks = [{"id": "a", "text": "向量数据库 向量数据库 向量数据库 向量索引", "title": ""},
                  {"id": "b", "text": "向量数据库", "title": ""}]
        kb.build(chunks)
        fa = [f for i, f in kb.postings.get("向量", []) if i == 0][0]
        fb = [f for i, f in kb.postings.get("向量", []) if i == 1][0]
        self.assertGreater(fa, fb)
        self.assertEqual(fa, 4)  # 二元组跨词连排：3 个"向量数据库"+1 个"向量索引"


class TestBloomLadder(unittest.TestCase):
    def test_promotes_every_three_correct(self):
        card = {"interval": 1, "reps": 0, "streak": 0, "bloom": "记忆"}
        for _ in range(3):
            card = sm2_next(card, 5, 3.0)
        self.assertEqual(card["bloom"], "理解")
        for _ in range(3):
            card = sm2_next(card, 5, 3.0)
        self.assertEqual(card["bloom"], "应用")
        for _ in range(3):
            card = sm2_next(card, 5, 3.0)
        self.assertEqual(card["bloom"], "分析")
        # 顶层不再越级
        for _ in range(3):
            card = sm2_next(card, 5, 3.0)
        self.assertEqual(card["bloom"], "分析")
        self.assertIn(card["bloom"], BLOOM_LEVELS)

    def test_wrong_answer_resets(self):
        card = {"interval": 1, "reps": 0, "streak": 0, "bloom": "记忆"}
        for _ in range(3):
            card = sm2_next(card, 5, 3.0)
        self.assertEqual(card["bloom"], "理解")
        card = sm2_next(card, 2, 3.0)  # 答错
        self.assertEqual(card["streak"], 0)
        card = sm2_next(card, 5, 3.0)
        self.assertEqual(card["bloom"], "理解")  # 未满 3 次不晋升

    def test_store_record_and_mastery(self):
        with tempfile.TemporaryDirectory() as td:
            store = ReviewStore(Path(td) / "review_state.json")
            for _ in range(3):
                store.record("kp1", "乐观锁", "Q", "A", True)
            self.assertEqual(store.cards["kp1"]["bloom"], "理解")
            m = store.mastery()["kp1"]
            self.assertEqual(m["next_bloom"], "理解")


if __name__ == "__main__":
    unittest.main()
