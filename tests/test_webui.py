"""图形界面：客观题判分、主观题评审、HTTP 服务冒烟。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from course_agent.webui import (Handler, grade_objective, grade_subjective,
                                _norm_text)


class TestGradeObjective(unittest.TestCase):
    def test_single_choice(self):
        ex = {"type": "single_choice", "answer": "B"}
        self.assertEqual(grade_objective(ex, "B"), (True, grade_objective(ex, "B")[1]))
        self.assertFalse(grade_objective(ex, "A")[0])
        self.assertFalse(grade_objective(ex, "")[0])

    def test_multi_choice(self):
        ex = {"type": "multi_choice", "answer": "AC"}
        self.assertTrue(grade_objective(ex, "AC")[0])
        self.assertTrue(grade_objective(ex, "CA")[0])  # 顺序无关
        self.assertFalse(grade_objective(ex, "A")[0])
        self.assertFalse(grade_objective(ex, "ABC")[0])

    def test_single_choice_text_options_no_substring_swallow(self):
        # 答案存成选项文本时的回退映射：精确匹配优先，
        # "正确"/"不正确" 这种互为子串的选项不得误判
        ex = {"type": "single_choice", "answer": "正确",
              "options": ["不正确", "正确"]}
        ok, note = grade_objective(ex, "B")
        self.assertTrue(ok)
        self.assertIn("B", note)
        self.assertFalse(grade_objective(ex, "A")[0])
        # 唯一包含匹配仍可用
        ex2 = {"type": "single_choice", "answer": "对 HTTP 响应码",
               "options": ["HTTP 请求头", "HTTP 响应码", "TCP 端口"]}
        self.assertTrue(grade_objective(ex2, "B")[0])

    def test_true_false_variants(self):
        ex = {"type": "true_false", "answer": "对"}
        self.assertTrue(grade_objective(ex, "对")[0])
        self.assertTrue(grade_objective(ex, "正确")[0])
        self.assertTrue(grade_objective(ex, "是")[0])
        self.assertFalse(grade_objective(ex, "错")[0])
        ex2 = {"type": "true_false", "answer": "错"}
        self.assertTrue(grade_objective(ex2, "错误")[0])

    def test_fill_blank_lenient(self):
        ex = {"type": "fill_blank", "answer": "version"}
        self.assertTrue(grade_objective(ex, "version")[0])
        self.assertTrue(grade_objective(ex, " Version ")[0])
        self.assertFalse(grade_objective(ex, "verson")[0])

    def test_unanswered(self):
        self.assertFalse(grade_objective({"type": "fill_blank", "answer": "x"}, "")[0])


class _FakeLLM:
    def chat(self, system, user, **kw):
        if "教研老师" in system:  # 交卷后的测验解析与强化
            return json.dumps({"overall": "整体掌握不错，个别概念需巩固",
                               "items": [{"idx": 0, "analysis": "第1题解析"}],
                               "weak_points": [{"topic": "事务回滚", "content": "强化讲解"}],
                               "suggestions": ["复习 rollbackFor"]})
        assert "学生代码" in system or "学生回答" in system
        return json.dumps({"score": 80, "verdict": "部分掌握", "feedback": "还差一个要点"})


class TestGradeSubjective(unittest.TestCase):
    def test_coding_review(self):
        ex = {"type": "coding_practice", "question": "实现 toggle",
              "answer": "function toggle(){}", "check_points": ["状态切换"]}
        r = grade_subjective(_FakeLLM(), ex, "function toggle() { /* ... */ }")
        self.assertEqual(r["score"], 80)
        self.assertEqual(r["verdict"], "部分掌握")

    def test_short_answer(self):
        ex = {"type": "short_answer", "question": "简述", "answer": "参考"}
        r = grade_subjective(_FakeLLM(), ex, "学生回答")
        self.assertEqual(r["feedback"], "还差一个要点")


class TestServerSmoke(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cfg_out = Path(self._td.name) / "output"
        course = self.cfg_out / "webui" / "demo-001"
        course.mkdir(parents=True)
        (course / "session.json").write_text(json.dumps({
            "title": "测试课", "passed": True, "qa_score": 90,
            "sections": 1, "exercises": 2}, ensure_ascii=False), encoding="utf-8")
        (course / "sections.json").write_text(json.dumps([{
            "title": "第一节",
            "knowledge_points": [{"title": "要点", "detail": "细节", "time": ""}],
            "key_nodes": [],
            "exercises": [
                {"type": "true_false", "question": "判断题", "answer": "对",
                 "explanation": "", "difficulty": "easy"},
                {"type": "short_answer", "question": "简答题", "answer": "参考",
                 "explanation": "", "difficulty": "medium"},
            ]}], ensure_ascii=False), encoding="utf-8")
        (course / "notes.md").write_text("# 笔记", encoding="utf-8")
        (course / "review_report.md").write_text("# 报告", encoding="utf-8")

        Handler.cfg = type("C", (), {"output_dir": str(self.cfg_out),
                                     "data_dir": str(self.cfg_out.parent / "data")})()
        Handler.llm = _FakeLLM()
        from course_agent.webui import ThreadingHTTPServer as S
        self.srv = S(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._td.cleanup()

    def test_index_and_courses(self):
        html = urlopen(f"http://127.0.0.1:{self.port}/", timeout=5).read().decode("utf-8")
        self.assertIn("CourseAgent 课程助手", html)
        d = json.loads(urlopen(f"http://127.0.0.1:{self.port}/api/courses", timeout=5).read())
        self.assertEqual(len(d["courses"]), 1)
        self.assertEqual(d["courses"][0]["title"], "测试课")

    def test_course_detail_and_grade(self):
        import urllib.request
        d = json.loads(urlopen(f"http://127.0.0.1:{self.port}/api/courses", timeout=5).read())
        cid = d["courses"][0]["id"]
        c = json.loads(urlopen(f"http://127.0.0.1:{self.port}/api/course?id={cid}", timeout=5).read())
        self.assertEqual(len(c["sections"][0]["exercises"]), 2)

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/grade",
            data=json.dumps({"course": cid, "section": 0,
                             "answers": ["对", "我的回答"]}).encode(),
            headers={"Content-Type": "application/json"})
        r = json.loads(urlopen(req, timeout=30).read())
        self.assertTrue(r["results"][0]["correct"])
        self.assertEqual(r["results"][1]["score"], 80)  # 主观题走 FakeLLM
        self.assertEqual(r["summary"]["correct"], 2)
        # 交卷后自动生成解析与强化知识
        self.assertIn("整体", r["review"]["overall"])
        self.assertTrue(r["review"]["weak_points"])
        # 并追加保存到课程的 强化复习.md
        self.assertTrue((Path(self.cfg_out) / cid / "强化复习.md").is_file())
        # 进度已保存
        p = json.loads(urlopen(
            f"http://127.0.0.1:{self.port}/api/progress?course={cid}", timeout=5).read())
        self.assertEqual(p["sections"]["0"]["0"]["user"], "对")


if __name__ == "__main__":
    unittest.main()
