"""重复课程去重：判重规则、保留策略、组引用清理的回归测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import dedup_courses as dd  # noqa: E402


class TestNormTitle(unittest.TestCase):
    """归一化标题：同一门课的多种写法必须归一为同一键。"""

    def test_same_title(self):
        self.assertEqual(dd.norm_title("33、langchain-MCP调用"),
                         dd.norm_title("33、langchain-MCP调用"))

    def test_recording_seq_prefix_ignored(self):
        # 131 是录制序号，88 才是课号
        self.assertEqual(dd.norm_title("131-88-作业讲评-0928_0921"),
                         dd.norm_title("88、作业讲评 0928_0921"))

    def test_date_suffix_ignored(self):
        self.assertEqual(
            dd.norm_title("32、langchain-agent及FunctionCalling"),
            dd.norm_title("32、langchain-agent及FunctionCalling日期: 2026-10-28 09:12:37"))

    def test_different_courses_stay_different(self):
        self.assertNotEqual(dd.norm_title("18、langchain-模型调用及配置文件读取"),
                            dd.norm_title("19、langchain-模型调用及动态切换"))
        self.assertNotEqual(dd.norm_title("大模型概述-LLM常识"),
                            dd.norm_title("大模型概述-提示词工程"))


class TestCrmCode(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(dd.crm_code("https://meeting.tencent.com/crm/KmkBjrvx2b"),
                         "KmkBjrvx2b")
        self.assertEqual(dd.crm_code("https://meeting.tencent.com/crm/2kqXE3Bn06/QCBC/标题"),
                         "2kqXE3Bn06")
        self.assertEqual(dd.crm_code("data/harvest_transcripts/x.json"), "")


class _Fixture:
    """在临时目录里造若干课程产物目录。"""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.out = Path(self._td.name) / "output"
        self.out.mkdir(parents=True)

    def add(self, rel: str, title: str, source: str = "", score: int = 0,
            passed: bool = False, sections: int = 0, exercises: int = 0) -> Path:
        d = self.out / rel
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "title": title, "source": source, "qa_score": score,
            "passed": passed, "sections": sections, "exercises": exercises,
        }, ensure_ascii=False), encoding="utf-8")
        (d / "sections.json").write_text("[]", encoding="utf-8")
        return d

    def cleanup(self):
        self._td.cleanup()


class TestFindDuplicates(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def _groups(self):
        courses = [dd.course_info(d) for d in dd.iter_courses(self.fx.out)]
        return dd.find_duplicate_groups(courses)

    def test_no_dup_for_distinct_courses(self):
        self.fx.add("a", "第1课", "https://meeting.tencent.com/crm/AAA", 90, True)
        self.fx.add("b", "第2课", "https://meeting.tencent.com/crm/BBB", 90, True)
        self.assertEqual(self._groups(), [])

    def test_same_title_is_dup(self):
        self.fx.add("a", "同一门课", "https://meeting.tencent.com/crm/AAA", 90, True)
        self.fx.add("b", "同一门课", "https://meeting.tencent.com/crm/BBB", 80, True)
        groups = self._groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_same_source_is_dup(self):
        self.fx.add("a", "标题写法一", "https://meeting.tencent.com/crm/SAME", 90, True)
        self.fx.add("b", "标题写法二", "https://meeting.tencent.com/crm/SAME", 80, True)
        groups = self._groups()
        self.assertEqual(len(groups), 1)

    def test_transitive_grouping(self):
        # a 与 b 同标题，b 与 c 同来源 → 三者算一组
        self.fx.add("a", "X课", "https://meeting.tencent.com/crm/AAA", 90, True)
        self.fx.add("b", "X课", "https://meeting.tencent.com/crm/BBB", 80, True)
        self.fx.add("c", "Y课", "https://meeting.tencent.com/crm/BBB", 70, True)
        groups = self._groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_rank_prefers_passed_then_score(self):
        """保留策略：通过 > 得分 > 章节 > 习题 > 新。"""
        good = {"passed": True, "score": 90, "sections": 1, "exercises": 1, "mtime": 1}
        weak = {"passed": True, "score": 80, "sections": 9, "exercises": 9, "mtime": 2}
        failed = {"passed": False, "score": 99, "sections": 9, "exercises": 9, "mtime": 3}
        self.assertGreater(dd.rank_key(good), dd.rank_key(weak))
        self.assertGreater(dd.rank_key(weak), dd.rank_key(failed))


class TestSideEffects(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_drop_from_groups(self):
        gdir = self.fx.out / "课程整合" / "组A"
        gdir.mkdir(parents=True)
        (gdir / "组.json").write_text(json.dumps(
            {"name": "组A", "courses": ["keep1", "drop1", "keep2"]}), encoding="utf-8")
        touched = dd.drop_from_groups(self.fx.out, {"drop1"}, dry=False)
        self.assertEqual(touched, ["组A"])
        data = json.loads((gdir / "组.json").read_text(encoding="utf-8"))
        self.assertEqual(data["courses"], ["keep1", "keep2"])

    def test_drop_from_groups_dry_run_does_not_write(self):
        gdir = self.fx.out / "课程整合" / "组A"
        gdir.mkdir(parents=True)
        (gdir / "组.json").write_text(json.dumps(
            {"name": "组A", "courses": ["keep1", "drop1"]}), encoding="utf-8")
        dd.drop_from_groups(self.fx.out, {"drop1"}, dry=True)
        data = json.loads((gdir / "组.json").read_text(encoding="utf-8"))
        self.assertIn("drop1", data["courses"])   # 预览模式不改文件

    def test_move_to_trash_and_restore(self):
        d1 = self.fx.add("webui/dup1", "重复课", "u", 50, False)
        orig_trash = dd.TRASH_ROOT
        try:
            dd.TRASH_ROOT = self.fx.out.parent / "trash"
            dest = dd.move_to_trash([d1], self.fx.out, dry=False)
            self.assertFalse(d1.exists())                       # 已移走
            self.assertTrue((dest / "webui" / "dup1" / "session.json").is_file())
            # 还原
            n = dd.restore(dest, self.fx.out)
            self.assertEqual(n, 1)
            self.assertTrue((self.fx.out / "webui" / "dup1" / "session.json").is_file())
        finally:
            dd.TRASH_ROOT = orig_trash


class TestGhostMembers(unittest.TestCase):
    """组.json 里指向已删课程的幽灵成员清理。"""

    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def _group(self, members):
        gdir = self.fx.out / "课程整合" / "组A"
        gdir.mkdir(parents=True)
        gp = gdir / "组.json"
        gp.write_text(json.dumps({"name": "组A", "courses": members}),
                      encoding="utf-8")
        return gp

    def test_detect_ghosts(self):
        self.fx.add("webui/alive", "还在的课")
        gp = self._group(["webui/alive", "webui/deleted", "top-level-gone"])
        alive = {"webui/alive", "also-alive"}
        ghosts = dd.find_ghost_members(self.fx.out, alive)
        self.assertEqual(len(ghosts), 2)
        self.assertEqual({m for _, m, _ in ghosts},
                         {"webui/deleted", "top-level-gone"})

    def test_no_false_positive_for_webui_prefixed_id(self):
        """组.json 存目录名时也应认得出来，不误报。"""
        self._group(["alive-course"])
        ghosts = dd.find_ghost_members(self.fx.out, {"webui/alive-course"})
        self.assertEqual(ghosts, [])

    def test_clean_dry_run_keeps_file(self):
        gp = self._group(["keep", "ghost"])
        ghosts = dd.find_ghost_members(self.fx.out, {"keep"})
        dd.clean_ghosts(ghosts, dry=True)
        self.assertIn("ghost", json.loads(gp.read_text(encoding="utf-8"))["courses"])

    def test_clean_removes_only_ghosts(self):
        gp = self._group(["keep", "ghost1", "ghost2"])
        ghosts = dd.find_ghost_members(self.fx.out, {"keep"})
        touched = dd.clean_ghosts(ghosts, dry=False)
        self.assertEqual(len(touched), 1)
        self.assertEqual(json.loads(gp.read_text(encoding="utf-8"))["courses"],
                         ["keep"])

    def test_clean_is_idempotent(self):
        gp = self._group(["keep"])
        self.assertEqual(dd.find_ghost_members(self.fx.out, {"keep"}), [])
        dd.clean_ghosts([], dry=False)
        self.assertEqual(json.loads(gp.read_text(encoding="utf-8"))["courses"],
                         ["keep"])


if __name__ == "__main__":
    unittest.main()
