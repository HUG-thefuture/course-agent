"""图形界面：客观题判分、主观题评审、HTTP 服务冒烟。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from course_agent.webui import (Handler, INDEX_HTML, grade_objective, grade_subjective,
                                _norm_text, parse_run_lines)


class TestParseRunLines(unittest.TestCase):
    """批量输入识别：链接 [提取码] [标题] / 链接|提取码|标题 / 注释行。"""

    URL = "https://meeting.tencent.com/crm/abcd1234"

    def test_bare_link(self):
        items = parse_run_lines(self.URL)
        self.assertEqual(items, [{"source": self.URL, "password": "", "title": ""}])

    def test_space_format_with_code_and_title(self):
        items = parse_run_lines(self.URL + " QSZI 大模型概述")
        self.assertEqual(items[0]["password"], "QSZI")
        self.assertEqual(items[0]["title"], "大模型概述")

    def test_space_format_code_then_title_chinese(self):
        items = parse_run_lines(self.URL + " HCKB")
        self.assertEqual(items[0]["password"], "HCKB")
        self.assertEqual(items[0]["title"], "")

    def test_pipe_format(self):
        items = parse_run_lines(self.URL + "|RRFZ|课程B|extra")
        self.assertEqual(items[0], {"source": self.URL, "password": "RRFZ", "title": "课程B"})

    def test_pipe_two_parts_is_password(self):
        """两段竖线：与 batch 清单同构，第二段是提取码（顺序 链接|码|标题）。"""
        items = parse_run_lines(self.URL + "|课程C")
        self.assertEqual(items[0], {"source": self.URL, "password": "课程C", "title": ""})

    def test_comments_and_blanks_ignored(self):
        text = f"# 注释行\n\n  \n{self.URL} YWNR\n"
        items = parse_run_lines(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["password"], "YWNR")

    def test_long_token_is_title_not_password(self):
        """超过 8 位非提取码形态，整段当标题。"""
        items = parse_run_lines(self.URL + " ABCDEFGHI 真标题")
        self.assertEqual(items[0]["password"], "")
        self.assertEqual(items[0]["title"], "ABCDEFGHI 真标题")

    def test_multi_line_mixed(self):
        text = (f"{self.URL} QSZI 课A\n"
                f"https://meeting.tencent.com/crm/xyz|RRFZ|课B\n"
                "# 注释\n"
                "some/dir/lecture.mp4 本地录屏")
        items = parse_run_lines(text)
        self.assertEqual(len(items), 3)
        self.assertEqual([i["title"] for i in items], ["课A", "课B", "本地录屏"])
        self.assertEqual(items[2]["source"], "some/dir/lecture.mp4")

    def test_non_url_line_kept_as_source(self):
        items = parse_run_lines("普通网页无法识别的整行")
        self.assertEqual(items[0]["source"], "普通网页无法识别的整行")

    # ---- 会议纪要式标签块 ----

    MEETING = ("录制: 27、langchain-向量存储Milvus概述\n"
               "日期: 2026-10-26 16:31:23\n"
               "录制文件：https://meeting.tencent.com/crm/24rZ6ekgbe\n"
               "密码：YFDT")

    def test_label_block_basic(self):
        """用户粘贴的会议纪要块：标题/日期/录制文件/密码。"""
        items = parse_run_lines(self.MEETING)
        self.assertEqual(items, [{
            "source": "https://meeting.tencent.com/crm/24rZ6ekgbe",
            "password": "YFDT", "title": "27、langchain-向量存储Milvus概述"}])

    def test_label_block_field_order_irrelevant(self):
        """块内字段顺序无关，缺标题也可（源必填）。"""
        text = (f"密码：QSZI\n录制文件: {self.URL}\n日期：2026-01-01 08:00:00")
        items = parse_run_lines(text)
        self.assertEqual(items, [{"source": self.URL, "password": "QSZI", "title": ""}])

    def test_label_blocks_separated_by_blank_or_numbered(self):
        """多个块：空行分隔，或下一条的编号"录制:"行直接分隔（无空行）。"""
        text = (self.MEETING + "\n"
                + "录制: 28、百炼平台\n录制文件：https://meeting.tencent.com/crm/bbbb\n"
                  "密码：ABCD\n")
        items = parse_run_lines(text)
        self.assertEqual(len(items), 2)
        self.assertEqual([i["title"] for i in items],
                         ["27、langchain-向量存储Milvus概述", "28、百炼平台"])
        self.assertEqual(items[1]["password"], "ABCD")

    def test_label_block_mixed_with_plain_lines(self):
        """标签块与旧单行/竖线格式混排：裸行独立成项，不被块吞掉。"""
        text = (f"{self.URL} QSZI 单行课\n"
                + self.MEETING + "\n\n"
                + f"https://meeting.tencent.com/crm/p1|RRFZ|竖线课")
        items = parse_run_lines(text)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], {"source": self.URL, "password": "QSZI", "title": "单行课"})
        self.assertEqual(items[1]["password"], "YFDT")
        self.assertEqual(items[2], {"source": "https://meeting.tencent.com/crm/p1",
                                    "password": "RRFZ", "title": "竖线课"})

    def test_label_block_title_only_becomes_source(self):
        """只有标题没有源的块：与旧行为一致，整串当 source 交下游报错。"""
        items = parse_run_lines("录制: 只有标题的一行")
        self.assertEqual(items[0]["source"], "只有标题的一行")

    def test_label_block_ignores_date_time(self):
        """时间/日期标签值不参与任何字段。"""
        text = (f"时间: 2026-10-26 16:31:23\n标题: 某课\n文件: {self.URL}\n提取码：AB")
        items = parse_run_lines(text)
        self.assertEqual(items[0], {"source": self.URL, "password": "AB", "title": "某课"})

    def test_bare_title_then_label_block(self):
        """腾讯会议导出的实际形态：裸标题行 + 日期/录制文件/密码 标签行。

        标题行必须与下面的块配对成一条，而不是自己变成一条无链接待办
        （曾导致用户粘贴 4 门课却只排了 4 条无效任务）。
        """
        text = ("32、langchain-agent及FunctionCalling\n"
                "日期: 2026-10-26 16:31:23\n"
                f"录制文件：{self.URL}\n"
                "密码：ABCD\n"
                "\n"
                "33、langchain-MCP调用\n"
                f"录制文件：https://meeting.tencent.com/crm/bbbb\n"
                "密码：EFGH\n")
        items = parse_run_lines(text)
        self.assertEqual(items, [
            {"source": self.URL, "password": "ABCD",
             "title": "32、langchain-agent及FunctionCalling"},
            {"source": "https://meeting.tencent.com/crm/bbbb", "password": "EFGH",
             "title": "33、langchain-MCP调用"},
        ])

    def test_bare_title_without_link_still_reported(self):
        """裸标题后面没有链接：仍作为 source 交给下游报错（不静默丢弃）。"""
        items = parse_run_lines("32、langchain-agent及FunctionCalling\n日期: 2026-10-26")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "32、langchain-agent及FunctionCalling")

    def test_label_free_three_line_blocks(self):
        """无标签三行式：标题 / 链接 / 提取码 各一行。

        用户直接从腾讯会议复制时常见这种形态（没有"录制文件：""密码："前缀）。
        解析器必须把三行合成一条，且标题不能自己变成一条无链接待办。
        """
        text = ("32、langchain-agent及FunctionCalling\n"
                f"{self.URL}\n"
                "ABCD\n"
                "\n"
                "33、langchain-MCP调用\n"
                "https://meeting.tencent.com/crm/bbbb\n"
                "EFGH\n")
        items = parse_run_lines(text)
        self.assertEqual(items, [
            {"source": self.URL, "password": "ABCD",
             "title": "32、langchain-agent及FunctionCalling"},
            {"source": "https://meeting.tencent.com/crm/bbbb", "password": "EFGH",
             "title": "33、langchain-MCP调用"},
        ])

    def test_bare_password_line_attaches_to_previous_link(self):
        """链接下面单独一行提取码：认领给上一条链接，不单独成项。"""
        items = parse_run_lines(f"{self.URL}\nQSZI\n")
        self.assertEqual(items, [{"source": self.URL, "password": "QSZI", "title": ""}])


class TestUniversalLabelParsing(unittest.TestCase):
    """普适性回归：标签名不能写死，应按「标签：值」结构 + 值的内容判断字段。

    真实案例（用户粘贴，2026-11-06）：
        3、langgraph-数据隔离&状态合并
        日期: 2026-11-05 15:56:43
        回顾文件：https://meeting.tencent.com/crm/2qr1V8ym08
        密码：6FTY
    旧解析器硬编码了「录制文件/文件/链接」等标签，「回顾文件」不在白名单，
    结果把整条拆成 2 条错误待办。
    """

    URL = "https://meeting.tencent.com/crm/2qr1V8ym08"

    def test_reported_case_review_file_label(self):
        text = ("3、langgraph-数据隔离&状态合并\n"
                "日期: 2026-11-05 15:56:43\n"
                f"回顾文件：{self.URL}\n"
                "密码：6FTY")
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "6FTY",
            "title": "3、langgraph-数据隔离&状态合并"}])

    def test_arbitrary_chinese_labels(self):
        """完全没见过的标签也能按值的内容识别。"""
        text = ("课程名称：Vue3 组件通信\n"
                f"回放地址：{self.URL}\n"
                "访问码：ABCD\n"
                "生成时间：2026-11-05")
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "ABCD", "title": "Vue3 组件通信"}])

    def test_english_labels(self):
        text = f"Title: My Course\nURL: {self.URL}\nPassword: X1Y2"
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "X1Y2", "title": "My Course"}])

    def test_label_order_irrelevant(self):
        """提取码/链接在前、编号标题在后：不能因标题以 N、开头就另起一条。"""
        text = (f"提取码：6FTY\n链接：{self.URL}\n"
                "录制: 5、langgraph-状态管理\n日期: 2026-11-05")
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "6FTY",
            "title": "5、langgraph-状态管理"}])

    def test_two_numbered_blocks_without_blank_line(self):
        text = ("1、课程甲\n链接：https://meeting.tencent.com/crm/AAAA\n密码：AAAA\n"
                "2、课程乙\n链接：https://meeting.tencent.com/crm/BBBB\n密码：BBBB")
        items = parse_run_lines(text)
        self.assertEqual(len(items), 2)
        self.assertEqual([i["title"] for i in items], ["1、课程甲", "2、课程乙"])

    def test_url_colon_not_treated_as_label(self):
        """`链接: https://...` 里 URL 的协议冒号不能被当成标签分隔符。"""
        self.assertEqual(parse_run_lines(f"链接: {self.URL}"),
                         [{"source": self.URL, "password": "", "title": ""}])

    def test_title_containing_colon(self):
        """标题里含冒号时，只按第一个冒号拆标签，剩余部分仍是标题。"""
        text = f"课程: Spring Boot: 事务与并发\n链接: {self.URL}\n密码: ABCD"
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "ABCD",
            "title": "Spring Boot: 事务与并发"}])

    def test_local_file_path_as_source(self):
        text = "课程: 本地课\n素材文件: D:\\课程\\lecture.srt"
        self.assertEqual(parse_run_lines(text), [{
            "source": "D:\\课程\\lecture.srt", "password": "", "title": "本地课"}])

    def test_unknown_label_with_date_value_ignored(self):
        """未知标签 + 日期值：按值判为日期而忽略，不污染标题。"""
        text = f"任意字段: 2026-11-05 15:56\n回顾: {self.URL}\n验证码: 6FTY"
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "6FTY", "title": ""}])

    def test_fullwidth_colon_labels(self):
        text = f"标题：某课程\n回放文件：{self.URL}\n密码：ABC1"
        self.assertEqual(parse_run_lines(text), [{
            "source": self.URL, "password": "ABC1", "title": "某课程"}])


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

    def test_failed_courses_hidden_by_default(self):
        # 未过质检（生成失败）的课不出现在默认列表；?all=1 可见
        bad = Path(self.cfg_out) / "webui" / "bad-002"
        bad.mkdir(parents=True)
        (bad / "session.json").write_text(json.dumps(
            {"title": "失败课", "passed": False, "qa_score": 40,
             "sections": 1, "exercises": 1}, ensure_ascii=False), encoding="utf-8")
        (bad / "sections.json").write_text("[]", encoding="utf-8")
        d = json.loads(urlopen(f"http://127.0.0.1:{self.port}/api/courses", timeout=5).read())
        self.assertNotIn("失败课", [c["title"] for c in d["courses"]])
        d2 = json.loads(urlopen(
            f"http://127.0.0.1:{self.port}/api/courses?all=1", timeout=5).read())
        self.assertIn("失败课", [c["title"] for c in d2["courses"]])

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


class TestPageScript(unittest.TestCase):
    """内嵌前端脚本语法校验：曾因 onclick 里未转义的引号嵌套
    （show('courses') 放进单引号字符串）导致整段 <script> 报
    SyntaxError，页面所有功能一起失效。"""

    def test_inline_js_parses(self):
        import re
        import shutil
        import subprocess

        m = re.search(r"<script>(.*)</script>", INDEX_HTML, re.S)
        self.assertIsNotNone(m, "页面缺少 <script> 块")
        js = m.group(1)
        node = shutil.which("node")
        if not node:
            self.skipTest("无 node，跳过 JS 语法校验")
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                         delete=False) as f:
            f.write(js)
            path = f.name
        try:
            r = subprocess.run([node, "--check", path],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"内嵌 JS 语法错误:\n{r.stderr}")
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
