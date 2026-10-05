"""回归：merge_courses 课号提取（录制序号 ≠ 课程编号）。"""

import unittest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.merge_courses import course_number, group_of  # noqa: E402


class TestCourseNumber(unittest.TestCase):
    def test_two_segment_dash_uses_second(self):
        # 072 是录制目录序号，35 才是课号
        self.assertEqual(course_number("072-35-新闻APP获取文章详情接口-0827_1030"), 35)
        self.assertEqual(course_number("094-56-DataFrame常用方法-0918_1400"), 56)

    def test_single_segment_punctuation_uses_first(self):
        self.assertEqual(course_number("45、request获取静态数据"), 45)
        self.assertEqual(course_number("3.HTML基础"), 3)

    def test_no_second_segment_returns_none(self):
        # 首段是录制序号、后跟非数字：不能拿录制序号当课号
        self.assertIsNone(course_number("001-人工智能产训特训-0720_1644"))
        self.assertIsNone(course_number("035-星星的个人会议室-0812_1000"))
        self.assertIsNone(course_number("Express 中间件详解"))


class TestGroupOf(unittest.TestCase):
    def test_numbered_courses_landed_by_course_number(self):
        self.assertEqual(group_of("094-56-DataFrame常用方法-0918_1400"),
                         "数据分析与可视化（Pandas 栈）")
        self.assertEqual(group_of("45、request获取静态数据"),
                         "爬虫与数据采集")

    def test_keyword_fallback_not_blocked_by_recording_seq(self):
        # 旧实现拿录制序号 001 命中 Web 前端组，"人工智能产训" 关键词永远轮不到
        self.assertEqual(group_of("001-人工智能产训特训-0720_1644"),
                         "人工智能产训特训营")
        self.assertEqual(group_of("035-星星的个人会议室-0812_1000"),
                         "人工智能产训特训营")
        self.assertEqual(group_of("061-20-Express路由与中间件-0901_0900"),
                         "Node/Express 后端与 Ajax")


if __name__ == "__main__":
    unittest.main()
