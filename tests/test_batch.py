"""批量处理：发现、逐项隔离、断点续跑、转写缓存。"""

import json
import tempfile
import unittest
from pathlib import Path

from course_agent.batch import (BatchItem, discover_items, run_batch)
from course_agent.config import AgentConfig

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TXT = ROOT / "examples" / "sample_lecture.txt"
EXAMPLE_SRT = ROOT / "examples" / "sample_lecture.srt"


class TestDiscover(unittest.TestCase):
    def test_directory_scan(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "第1课.txt").write_text("内容", encoding="utf-8")
            (d / "第2课.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n",
                                         encoding="utf-8")
            (d / "第3课转写.json").write_text(
                json.dumps([{"start": 0.0, "text": "转写内容"}], ensure_ascii=False),
                encoding="utf-8")
            (d / "harvest_report.json").write_text("{}", encoding="utf-8")
            (d / "忽略.xyz").write_text("x", encoding="utf-8")
            (d / ".hidden.txt").write_text("x", encoding="utf-8")
            items = discover_items([str(d)])
            self.assertEqual(len(items), 3)  # txt/srt/转写json，报告与隐藏文件排除
            self.assertEqual(items[2].title, "第3课转写")

    def test_text_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "courses.list"
            m.write_text(
                "# 注释行\n"
                "https://a.com/x.mp4\n"
                "https://meeting.tencent.com/crm/abc|HCKB|第3课\n"
                f"{EXAMPLE_TXT}|第4课\n", encoding="utf-8")
            items = discover_items([str(m)])
            self.assertEqual(len(items), 3)
            self.assertEqual(items[1].password, "HCKB")
            self.assertEqual(items[1].title, "第3课")
            self.assertEqual(items[2].title, "第4课")

    def test_single_txt_is_course_not_manifest(self):
        items = discover_items([str(EXAMPLE_TXT)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "sample_lecture")

    def test_json_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "courses.json"
            m.write_text(json.dumps([
                {"source": "https://a.com/1.mp4", "password": "P1", "title": "课1"},
                "https://a.com/2.m3u8",
            ], ensure_ascii=False), encoding="utf-8")
            items = discover_items([str(m)])
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].password, "P1")
            self.assertEqual(items[0].title, "课1")
            self.assertEqual(items[1].source, "https://a.com/2.m3u8")

    def test_url_and_single_file(self):
        items = discover_items(["https://a.com/b.mp4", str(EXAMPLE_TXT)])
        self.assertEqual(items[0].source, "https://a.com/b.mp4")
        self.assertEqual(items[1].title, "sample_lecture")


class TestRunBatch(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.cfg = AgentConfig()
        self.cfg.data_dir = str(self.td / "data")
        self.cfg.pipeline.partial_chars = 50000  # 示例材料按完整课处理

    def tearDown(self):
        self._td.cleanup()

    def test_batch_two_courses_and_report(self):
        from course_agent.llm import MockLLM
        out = run_batch(MockLLM(), self.cfg,
                        [BatchItem(str(EXAMPLE_TXT), "课程甲"),
                         BatchItem(str(EXAMPLE_SRT), "课程乙")],
                        out_root=self.td / "out")
        self.assertEqual([o.status for o in out], ["passed", "passed"])
        report = json.loads((self.td / "out" / "batch_report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["passed"], 2)
        self.assertTrue((self.td / "out" / "01-课程甲" / "notes.md").is_file())
        self.assertTrue((self.td / "out" / "02-课程乙" / "notes.md").is_file())

    def test_item_failure_is_isolated(self):
        from course_agent.llm import MockLLM
        out = run_batch(MockLLM(), self.cfg,
                        [BatchItem("不存在_9x.mp4", "坏课程"),
                         BatchItem(str(EXAMPLE_TXT), "好课程")],
                        out_root=self.td / "out2")
        self.assertEqual(out[0].status, "failed")
        self.assertTrue(out[0].error)
        self.assertEqual(out[1].status, "passed")

    def test_resume_skip_and_force(self):
        from course_agent.llm import MockLLM
        items = [BatchItem(str(EXAMPLE_TXT), "课程甲")]
        run_batch(MockLLM(), self.cfg, items, out_root=self.td / "out3")
        out2 = run_batch(MockLLM(), self.cfg, items, out_root=self.td / "out3")
        self.assertEqual(out2[0].status, "skipped")
        out3 = run_batch(MockLLM(), self.cfg, items, out_root=self.td / "out3",
                         force=True)
        self.assertEqual(out3[0].status, "passed")

    def test_retry_until_pass_recovers_failures(self):
        # 前 15 次调用返回垃圾（覆盖首轮的 3 次完整尝试），之后恢复：
        # 持续迭代轮应让课程从 failed 转为 passed
        from course_agent.llm import MockLLM

        class _Recovering:
            name = "recovering"

            def __init__(self, bad_calls, inner):
                self.bad = bad_calls
                self.inner = inner

            def chat(self, system, user, **kw):
                if self.bad > 0:
                    self.bad -= 1
                    return "这不是合法输出"
                return self.inner.chat(system, user, **kw)

        self.cfg.pipeline.retry_until_pass = 1
        out = run_batch(_Recovering(15, MockLLM()), self.cfg,
                        [BatchItem(str(EXAMPLE_TXT), "课程甲")],
                        out_root=self.td / "out5")
        report = json.loads((self.td / "out5" / "batch_report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["retry_rounds"], 1)
        final = [o for o in report["outcomes"] if o["title"] == "课程甲"][0]
        self.assertEqual(final["status"], "passed")

    def test_retry_gives_up_after_rounds(self):
        from course_agent.llm import MockLLM
        self.cfg.pipeline.retry_until_pass = 1
        out = run_batch(MockLLM(fail_plan=999), self.cfg,
                        [BatchItem("不存在_9x.mp4", "坏课程")],
                        out_root=self.td / "out5b")
        self.assertEqual(out[0].status, "failed")
        report = json.loads((self.td / "out5b" / "batch_report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["retry_rounds"], 1)  # 轮数用尽后停止

    def test_retry_covers_untitled_items(self):
        # 无标题条目（如裸 URL）也必须进重试：旧实现按 (source, title="") 查
        # outcome_map，而失败条目的标题是解析出来的 slug，永远查不到、一次不重试
        from course_agent.llm import MockLLM
        self.cfg.pipeline.retry_until_pass = 1
        out = run_batch(MockLLM(fail_plan=999), self.cfg,
                        [BatchItem("不存在_9x.mp4")],  # 注意：无标题
                        out_root=self.td / "out5c")
        self.assertEqual(out[0].status, "failed")
        report = json.loads((self.td / "out5c" / "batch_report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["retry_rounds"], 1)
        self.assertEqual(len(report["outcomes"]), 1)  # 不残留重复条目

    def test_transcribe_cache(self):
        import course_agent.batch as batch_mod
        calls = {"n": 0}
        orig = batch_mod.transcribe_media

        def fake_transcribe(path, **kw):
            calls["n"] += 1
            from course_agent.pipeline.models import Segment
            return [Segment(start=0.0, text="缓存测试内容。这是第1段。"),
                    Segment(start=1.0, text="这是第2段，讲一点东西。")]

        batch_mod.transcribe_media = fake_transcribe
        try:
            media = self.td / "fake.mp4"
            media.write_bytes(b"fake-video")
            item = BatchItem(str(media), "视频课")
            run_batch(__import__("course_agent.llm", fromlist=["MockLLM"]).MockLLM(),
                      self.cfg, [item], out_root=self.td / "out4")
            run_batch(__import__("course_agent.llm", fromlist=["MockLLM"]).MockLLM(),
                      self.cfg, [item], out_root=self.td / "out4",
                      force=True)  # 强制重跑但应命中转写缓存
        finally:
            batch_mod.transcribe_media = orig
        self.assertEqual(calls["n"], 1)  # 第二次命中缓存，不再转写


if __name__ == "__main__":
    unittest.main()
