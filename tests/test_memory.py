"""记忆库：会话、范例召回、容量上限、反馈。"""

import tempfile
import unittest
from pathlib import Path

from course_agent.memory import MAX_EXAMPLES, Memory


class TestMemory(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "memory.db"
        self.mem = Memory(self.db)

    def tearDown(self):
        self.mem.close()
        self._td.cleanup()

    def test_save_session(self):
        self.mem.save_session("s1", "课", "src", True, 90.0)
        self.mem.save_session("s2", "课2", "src", False, 40.0)
        # 主键去重
        self.mem.save_session("s1", "课", "src", True, 95.0)

    def test_example_recall(self):
        self.mem.add_example("s1", "事务失效场景", "自调用导致事务失效，因为绕过了代理对象",
                             "{}", 95.0)
        self.mem.add_example("s1", "消息队列", "rocketmq 削峰填谷与最终一致性",
                             "{}", 88.0)
        rows = self.mem.recall_examples("事务失效 代理对象", k=2)
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("事务", rows[0]["section_title"])
        self.assertEqual(self.mem.count_examples(), 2)

    def test_recall_no_match(self):
        self.mem.add_example("s1", "完全无关主题", "完全没有交集的内容文本", "{}", 90)
        self.assertEqual(self.mem.recall_examples("分布式锁 redisson", k=2), [])

    def test_prune_cap(self):
        for i in range(MAX_EXAMPLES + 20):
            self.mem.add_example(f"s{i}", f"节{i}", f"内容{i}", "{}", 50.0)
        self.assertEqual(self.mem.count_examples(), MAX_EXAMPLES)

    def test_all_examples(self):
        self.mem.add_example("s1", "节", "内容", '{"a": 1}', 90)
        rows = self.mem.all_examples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["output_json"], '{"a": 1}')

    def test_feedback(self):
        self.mem.add_feedback("s1", "第一节", 5, "很清楚")
        self.mem.add_feedback("s1", "", 2, "习题太简单")

    def test_context_manager(self):
        with Memory(self.db) as m:
            m.save_session("sx", "t", "src", True, 80)

    def test_parent_dir_created(self):
        deep = Path(self._td.name) / "a" / "b" / "memory.db"
        with Memory(deep) as m:
            m.save_session("s", "t", "src", True, 70)
        self.assertTrue(deep.is_file())


if __name__ == "__main__":
    unittest.main()
