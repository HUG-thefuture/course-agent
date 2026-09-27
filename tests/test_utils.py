"""utils：JSON 提取、分词相似度、时间格式化、编码读取。"""

import unittest

from course_agent.utils import (extract_json, fmt_time, safe_read_text,
                                similarity, slugify, tokenize, truncate)


class TestExtractJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_markdown_fence(self):
        text = '```json\n{"a": [1, 2], "b": "x"}\n```'
        self.assertEqual(extract_json(text), {"a": [1, 2], "b": "x"})

    def test_surrounding_prose(self):
        text = '好的，以下是结果：\n{"passed": true, "issues": []}\n希望有帮助'
        self.assertEqual(extract_json(text), {"passed": True, "issues": []})

    def test_braces_inside_strings(self):
        text = '{"s": "包含 } 和 { 的字符串", "n": 2}'
        self.assertEqual(extract_json(text)["n"], 2)

    def test_nested(self):
        text = '{"outer": {"inner": [{"x": 1}]}}'
        self.assertEqual(extract_json(text)["outer"]["inner"][0]["x"], 1)

    def test_array(self):
        self.assertEqual(extract_json('[1, "a", {"b": 2}]'), [1, "a", {"b": 2}])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            extract_json("完全没有 JSON")
        with self.assertRaises(ValueError):
            extract_json("")
        with self.assertRaises(ValueError):
            extract_json('{"a": ')


class TestTokenizeSimilarity(unittest.TestCase):
    def test_chinese_overlap(self):
        a = "事务失效的三大场景"
        b = "Spring 事务失效场景总结"
        self.assertGreater(similarity(a, b), 0.1)

    def test_disjoint(self):
        self.assertEqual(similarity("数据库索引", "rocketmq 消息队列"), 0.0)

    def test_empty(self):
        self.assertEqual(similarity("", "abc"), 0.0)
        self.assertEqual(tokenize(""), set())

    def test_english_word(self):
        self.assertIn("spring", tokenize("Spring Boot 教程"))


class TestMisc(unittest.TestCase):
    def test_fmt_time(self):
        self.assertEqual(fmt_time(65), "01:05")
        self.assertEqual(fmt_time(3675), "1:01:15")
        self.assertEqual(fmt_time(None), "")
        self.assertEqual(fmt_time(0), "00:00")

    def test_slugify(self):
        self.assertEqual(slugify("第1节: 事务/失效?"), "第1节-事务-失效")
        self.assertEqual(slugify("   "), "course")

    def test_truncate(self):
        self.assertEqual(truncate("abcdef", 3), "abc…")
        self.assertEqual(truncate("abc", 5), "abc")

    def test_safe_read_gbk(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gbk.txt"
            p.write_bytes("中文内容测试".encode("gbk"))
            self.assertEqual(safe_read_text(p), "中文内容测试")


if __name__ == "__main__":
    unittest.main()
