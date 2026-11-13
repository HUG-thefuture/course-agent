"""测验作答缓存回归测试。

用户报告（2026-11-13）：做完「复习题」后作答没有自动缓存，再次返回课程
页面全是空白。

根因（本文件锁定为回归契约）：
1. 前端 renderQuiz() 从不回显 DATA.progress，重进课程输入框/勾选全空；
2. POST /api/progress 的"深合并"只到章节级，切章/自动暂存只提交 `user`
   的增量会把该章已存的 score/correct 整个覆盖丢掉；
3. 交卷后的判分汇总（summaries）与解析（reviews）没有落盘，返回后不可见。
"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from course_agent.webui import Handler, INDEX_HTML, _merge_progress


class _FakeLLM:
    def chat(self, system, user, **kw):
        if "教研老师" in system:
            return json.dumps({"overall": "整体掌握不错",
                               "items": [{"idx": 0, "analysis": "第1题解析"}],
                               "weak_points": [{"topic": "要点", "content": "强化讲解"}],
                               "suggestions": ["复习要点"]})
        return json.dumps({"score": 80, "verdict": "部分掌握", "feedback": "还差一个要点"})


class TestMergeProgress(unittest.TestCase):
    """纯函数层：三层深合并必须保住已有判分字段。"""

    def test_user_only_update_keeps_score(self):
        data = {"sections": {"0": {"0": {"user": "对", "score": 100, "correct": True}}}}
        _merge_progress(data, {"sections": {"0": {"0": {"user": "B"}}}})
        rec = data["sections"]["0"]["0"]
        self.assertEqual(rec["user"], "B")           # 新作答覆盖
        self.assertEqual(rec["score"], 100)          # 判分保留
        self.assertTrue(rec["correct"])

    def test_other_section_untouched(self):
        data = {"sections": {"0": {"0": {"user": "A", "score": 100}}}}
        _merge_progress(data, {"sections": {"1": {"0": {"user": "B"}}}})
        self.assertEqual(data["sections"]["0"]["0"]["user"], "A")
        self.assertEqual(data["sections"]["1"]["0"]["user"], "B")

    def test_summaries_and_reviews_merge_by_section(self):
        data = {"summaries": {"0": {"avg": 90}}, "reviews": {"0": {"overall": "x"}}}
        _merge_progress(data, {"summaries": {"1": {"avg": 60}},
                               "reviews": {"1": {"overall": "y"}}})
        self.assertEqual(data["summaries"]["0"]["avg"], 90)
        self.assertEqual(data["summaries"]["1"]["avg"], 60)
        self.assertEqual(data["reviews"]["0"]["overall"], "x")

    def test_dirty_sections_payload_does_not_wipe(self):
        data = {"sections": {"0": {"0": {"user": "A", "score": 100}}}}
        _merge_progress(data, {"sections": "not-a-dict"})
        self.assertEqual(data["sections"]["0"]["0"]["score"], 100)

    def test_empty_increment_is_noop(self):
        data = {"sections": {"0": {"0": {"user": "A"}}}}
        before = json.loads(json.dumps(data))
        _merge_progress(data, {})
        self.assertEqual(data, before)


class TestQuizPersistenceHTTP(unittest.TestCase):
    """真实 HTTP 服务：交卷→暂存→再进课程 的完整链路。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.cfg_out = root / "output"
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        course = self.cfg_out / "webui" / "quiz-001"
        course.mkdir(parents=True)
        (course / "session.json").write_text(json.dumps({
            "title": "缓存测试课", "passed": True, "qa_score": 95,
            "sections": 2, "exercises": 3}, ensure_ascii=False), encoding="utf-8")
        (course / "sections.json").write_text(json.dumps([
            {"title": "第一节", "knowledge_points": [], "key_nodes": [],
             "exercises": [
                 {"type": "true_false", "question": "判断题", "answer": "对",
                  "difficulty": "easy"},
                 {"type": "short_answer", "question": "简答题", "answer": "参考",
                  "difficulty": "medium"}]},
            {"title": "第二节", "knowledge_points": [], "key_nodes": [],
             "exercises": [
                 {"type": "single_choice", "question": "单选题", "answer": "A",
                  "options": ["一", "二"], "difficulty": "easy"}]},
        ], ensure_ascii=False), encoding="utf-8")
        (course / "review.md").write_text("# 复习笔记", encoding="utf-8")

        Handler.cfg = type("C", (), {"output_dir": str(self.cfg_out),
                                     "data_dir": str(self.data_dir)})()
        Handler.llm = _FakeLLM()
        from course_agent.webui import ThreadingHTTPServer as S
        self.srv = S(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cid = "webui/quiz-001"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._td.cleanup()

    # -------------------------------------------------- helpers
    def _get(self, path):
        return json.loads(urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30).read())

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        return json.loads(urlopen(req, timeout=60).read())

    def _stage(self, section, data):
        return self._post("/api/progress", {"course": self.cid, "data": data})

    # -------------------------------------------------- tests
    def test_grade_persists_scores_and_review(self):
        r = self._post("/api/grade", {"course": self.cid, "section": 0,
                                      "answers": ["对", "我的回答"]})
        self.assertEqual(r["summary"]["correct"], 2)
        p = self._get("/api/progress?course=" + quote(self.cid))
        sec = p["sections"]["0"]
        self.assertEqual(sec["0"]["user"], "对")
        self.assertEqual(sec["0"]["score"], 100)
        self.assertTrue(sec["0"]["correct"])
        self.assertEqual(sec["1"]["score"], 80)      # 主观题走 FakeLLM
        # 汇总与解析也已落盘
        self.assertEqual(p["summaries"]["0"]["correct"], 2)
        self.assertIn("整体", p["reviews"]["0"]["overall"])
        self.assertTrue(p["reviews"]["0"]["weak_points"])

    def test_autosave_after_grade_keeps_scores(self):
        """核心回归：交卷后再自动暂存（只带 user）不得抹掉判分。"""
        self._post("/api/grade", {"course": self.cid, "section": 0,
                                  "answers": ["对", "我的回答"]})
        # 模拟前端 debounce 自动暂存 / 切章暂存：只提交作答文本
        self._stage("0", {"sections": {"0": {"0": {"user": "错"},
                                            "1": {"user": "改过的回答"}}}})
        p = self._get("/api/progress?course=" + quote(self.cid))
        sec = p["sections"]["0"]
        self.assertEqual(sec["0"]["user"], "错")       # 新作答生效
        self.assertEqual(sec["0"]["score"], 100)       # 但上次判分仍在
        self.assertTrue(sec["0"]["correct"])
        self.assertEqual(sec["1"]["user"], "改过的回答")
        self.assertEqual(sec["1"]["score"], 80)
        # 汇总/解析同样不能被这次暂存冲掉
        self.assertEqual(p["summaries"]["0"]["correct"], 2)
        self.assertTrue(p["reviews"]["0"].get("overall"))

    def test_cross_section_merge(self):
        self._stage("0", {"sections": {"0": {"0": {"user": "内容0"}}}})
        self._stage("1", {"sections": {"1": {"0": {"user": "A"}}}})
        p = self._get("/api/progress?course=" + quote(self.cid))
        self.assertEqual(p["sections"]["0"]["0"]["user"], "内容0")
        self.assertEqual(p["sections"]["1"]["0"]["user"], "A")

    def test_course_detail_exposes_progress_for_frontend_restore(self):
        self._post("/api/grade", {"course": self.cid, "section": 0,
                                  "answers": ["对", "我的回答"]})
        self._stage("1", {"sections": {"1": {"0": {"user": "A"}}}})
        c = self._get("/api/course?id=" + quote(self.cid))
        self.assertIn("progress", c)
        p = c["progress"]
        # 前端 restoreAnswers() 的数据源
        self.assertEqual(p["sections"]["0"]["0"]["user"], "对")
        self.assertEqual(p["sections"]["1"]["0"]["user"], "A")
        # 前端 renderReviewPanel() 的数据源
        self.assertIn("0", p["summaries"])
        self.assertIn("0", p["reviews"])

    def test_empty_and_dirty_payloads_tolerated(self):
        self.assertEqual(self._stage("0", {}).get("ok"), True)
        self.assertEqual(self._stage("0", {"sections": {}}).get("ok"), True)
        # 缺 course 时是 400 而不是 500
        try:
            self._post("/api/progress", {"data": {"sections": {}}})
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        else:
            self.fail("缺 course 应返回 400")

    def test_corrupt_progress_file_does_not_lose_new_writes(self):
        pdir = self.data_dir / "webui_progress"
        pdir.mkdir(parents=True, exist_ok=True)
        bad = pdir / "webui_quiz-001.json"
        bad.write_text('{"sections": {"0": ', encoding="utf-8")  # 截断的 JSON
        self._stage("0", {"sections": {"0": {"0": {"user": "新数据"}}}})
        p = self._get("/api/progress?course=" + quote(self.cid))
        self.assertEqual(p["sections"]["0"]["0"]["user"], "新数据")
        # 坏文件被备份而不是直接丢弃
        backups = list(pdir.glob("*.bad-*"))
        self.assertTrue(backups, "损坏的进度文件应被备份")


class TestFrontendRestoreHooks(unittest.TestCase):
    """前端脚本必须真的实现"回显 + 自动暂存"，否则服务端存了也没用。"""

    def setUp(self):
        import re
        m = re.search(r"<script>(.*)</script>", INDEX_HTML, re.S)
        self.assertIsNotNone(m)
        self.js = m.group(1)

    def test_render_quiz_restores_saved_answers(self):
        self.assertIn("restoreAnswers", self.js)
        self.assertIn("renderQuiz", self.js)
        # renderQuiz 内必须调用恢复逻辑，否则重进课程仍是空白
        body = self.js[self.js.index("function renderQuiz"):]
        body = body[:body.index("\nfunction ", 10)] if "\nfunction " in body[10:] else body
        self.assertIn("restoreAnswers", body)
        self.assertIn("renderScores", body)
        self.assertIn("renderReviewPanel", body)

    def test_auto_save_on_answer_change(self):
        self.assertIn("markDirty", self.js)
        self.assertIn("flushQuiz", self.js)
        self.assertIn("/api/progress", self.js)
        # 输入事件绑定 + 离开页面兜底
        self.assertIn("bindAnswerWatchers", self.js)
        self.assertIn("beforeunload", self.js)

    def test_leaving_course_flushes(self):
        # show() 离开答题页时必须先 flush
        self.assertIn('if(t!=="course"', self.js)
        # 换课时也 flush 上一门
        self.assertIn("COURSE!==id", self.js)


if __name__ == "__main__":
    unittest.main()
