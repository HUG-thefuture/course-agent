"""课程数据一致性体检工具的回归测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import audit_courses as ac  # noqa: E402


class _Fixture:
    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.out = Path(self._td.name) / "output"
        self.data = Path(self._td.name) / "data"
        self.out.mkdir()
        self.data.mkdir()
        (self.data / "webui_progress").mkdir()

    def add_course(self, rel: str, title: str, passed: bool = True,
                   score: int = 90, source: str = "") -> Path:
        d = self.out / rel
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "title": title, "passed": passed, "qa_score": score,
            "source": source, "sections": 2, "exercises": 4,
        }, ensure_ascii=False), encoding="utf-8")
        for f in ("notes.md", "sections.json"):
            (d / f).write_text("x", encoding="utf-8")
        return d

    def cleanup(self):
        self._td.cleanup()


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_clean_state(self):
        self.fx.add_course("a", "课程A")
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(r["courses"], 1)
        self.assertEqual(r["issues"]["duplicates"], [])
        self.assertEqual(r["issues"]["ghost_members"], [])
        self.assertEqual(r["issues"]["incomplete"], [])
        self.assertEqual(r["issues"]["failed_qa"], [])
        self.assertEqual(r["total_issues"], 0)

    def test_detects_duplicates(self):
        u = "https://meeting.tencent.com/crm/SAME"
        self.fx.add_course("a", "同一门课", source=u)
        self.fx.add_course("b", "同一门课", source=u)
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(len(r["issues"]["duplicates"]), 1)
        self.assertEqual(r["issues"]["duplicates"][0]["count"], 2)

    def test_detects_ghost_members(self):
        self.fx.add_course("webui/alive", "还在的课")
        gdir = self.fx.out / "课程整合" / "组A"
        gdir.mkdir(parents=True)
        (gdir / "组.json").write_text(json.dumps(
            {"name": "组A", "courses": ["webui/alive", "webui/gone"]},
            ensure_ascii=False), encoding="utf-8")
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(len(r["issues"]["ghost_members"]), 1)
        self.assertEqual(r["issues"]["ghost_members"][0]["member"], "webui/gone")

    def test_progress_naming_matches_webui(self):
        """进度文件名规则必须与 webui._progress_path 一致，否则误报孤儿。"""
        self.assertEqual(ac._progress_name("webui/2026-course"),
                         "webui_2026-course.json")
        self.assertEqual(ac._progress_name("01-070-课"),
                         "01-070-课.json")

    def test_no_false_orphan_for_webui_prefixed_progress(self):
        self.fx.add_course("webui/20261020-ABC", "某课")
        (self.fx.data / "webui_progress" / "webui_20261020-ABC.json").write_text(
            "{}", encoding="utf-8")
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(r["issues"]["orphan_progress"], [])

    def test_detects_orphan_progress(self):
        (self.fx.data / "webui_progress" / "totally-orphan.json").write_text(
            "{}", encoding="utf-8")
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertIn("totally-orphan.json", r["issues"]["orphan_progress"])

    def test_detects_stale_title_override(self):
        self.fx.add_course("a", "课程A")
        (self.fx.data / "course_titles.json").write_text(json.dumps(
            {"a": "课程A", "deleted-id": "已删课"}, ensure_ascii=False),
            encoding="utf-8")
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(len(r["issues"]["stale_title_overrides"]), 1)

    def test_detects_incomplete(self):
        d = self.fx.add_course("a", "课程A")
        (d / "notes.md").unlink()
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(len(r["issues"]["incomplete"]), 1)
        self.assertIn("notes.md", r["issues"]["incomplete"][0]["missing"])

    def test_detects_empty_dirs(self):
        (self.fx.out / "empty").mkdir()
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertIn("empty", r["issues"]["empty_dirs"])

    def test_failed_qa_is_soft_hint(self):
        """未通过质检只提示，不算结构性错误（退出码 0）。"""
        self.fx.add_course("a", "低分课", passed=False, score=60)
        r = ac.audit(self.fx.out, self.fx.data)
        self.assertEqual(len(r["issues"]["failed_qa"]), 1)
        hard = (len(r["issues"]["duplicates"]) + len(r["issues"]["ghost_members"])
                + len(r["issues"]["intra_group_duplicates"])
                + len(r["issues"]["incomplete"]))
        self.assertEqual(hard, 0)


if __name__ == "__main__":
    unittest.main()
