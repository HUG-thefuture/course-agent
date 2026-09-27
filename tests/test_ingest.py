"""素材接入：字幕解析、HTML 提取、分块、加载。"""

import tempfile
import unittest
from pathlib import Path

from course_agent.ingest import IngestError, chunk_material, load_source
from course_agent.ingest.parsers import extract_html, parse_subtitle, plain_to_segments
from course_agent.ingest.transcriber import TranscribeError, transcribe_media
from course_agent.pipeline.models import Material, Segment

SRT_SAMPLE = """1
00:00:01,000 --> 00:00:03,500
第一个知识点

2
00:01:02,120 --> 00:01:05,000
事务失效的场景
"""

VTT_SAMPLE = """WEBVTT

00:00.500 --> 00:02.000
< c > 大家好 < /c >

00:01:10.000 --> 00:01:12.500
并发问题讲解
"""


class TestSubtitle(unittest.TestCase):
    def test_srt(self):
        segs = parse_subtitle(SRT_SAMPLE)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0].start, 1.0)
        self.assertAlmostEqual(segs[1].start, 62.12)
        self.assertEqual(segs[1].text, "事务失效的场景")

    def test_vtt(self):
        segs = parse_subtitle(VTT_SAMPLE)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0].start, 0.5)
        self.assertEqual(segs[1].text, "并发问题讲解")

    def test_empty(self):
        self.assertEqual(parse_subtitle("no timestamps here"), [])


class TestHtml(unittest.TestCase):
    def test_extract(self):
        raw = ("<html><head><title>课程页</title>"
               "<script>var x = 1;</script><style>.a{}</style></head>"
               "<body><p>你好 世界</p><div>第二段内容</div></body></html>")
        title, text = extract_html(raw)
        self.assertEqual(title, "课程页")
        self.assertIn("你好 世界", text)
        self.assertIn("第二段内容", text)
        self.assertNotIn("var x", text)

    def test_broken_html(self):
        title, text = extract_html("<p>残缺HTML")
        self.assertIn("残缺HTML", text)


class TestChunking(unittest.TestCase):
    def test_merge_and_boundaries(self):
        segs = [Segment(start=i * 10.0, text="字" * 120) for i in range(5)]
        m = Material(title="t", kind="text", source="x", segments=segs)
        chunks = chunk_material(m, max_chars=300)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.text), 300)
        # 覆盖完整：拼接时只多了块内换行分隔符，字符本体不能丢
        got = sum(len(c.text.replace("\n", "")) for c in chunks)
        self.assertEqual(got, sum(len(s.text) for s in segs))

    def test_long_single_segment_split(self):
        segs = [Segment(start=None, text="句。" * 400)]
        m = Material(title="t", kind="text", source="x", segments=segs)
        chunks = chunk_material(m, max_chars=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.text) <= 300 for c in chunks))

    def test_timestamps_preserved(self):
        segs = [Segment(start=5.0, text="A" * 120), Segment(start=20.0, text="B" * 120)]
        m = Material(title="t", kind="text", source="x", segments=segs)
        chunks = chunk_material(m, max_chars=220)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0].start_time, 5.0)

    def test_empty_material(self):
        m = Material(title="t", kind="text", source="x", segments=[])
        with self.assertRaises(IngestError):
            chunk_material(m)

    def test_too_small_chunk_chars(self):
        m = Material(title="t", kind="text", source="x",
                     segments=[Segment(None, "abc")])
        with self.assertRaises(ValueError):
            chunk_material(m, max_chars=50)


class TestLoadSource(unittest.TestCase):
    def test_txt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "课程.txt"
            p.write_text("第一段内容。\n\n第二段内容。", encoding="utf-8")
            m = load_source(str(p))
            self.assertEqual(m.kind, "text")
            self.assertEqual(m.title, "课程")
            self.assertGreaterEqual(len(m.segments), 1)

    def test_unsupported_ext(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.xyz"
            p.write_text("data", encoding="utf-8")
            with self.assertRaises(IngestError):
                load_source(str(p))

    def test_missing_file(self):
        with self.assertRaises(IngestError):
            load_source("不存在文件_9x.txt")

    def test_bad_url(self):
        with self.assertRaises(IngestError):
            load_source("ftp://example.com/x")

    def test_transcribe_graceful_without_dep(self):
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is not None:
            self.skipTest("faster-whisper 已安装，跳过降级路径测试")
        with self.assertRaises(TranscribeError):
            transcribe_media("不存在的音频.mp3")

    def test_plain_to_segments(self):
        segs = plain_to_segments("第一句。第二句。第三句。\n\n新段落")
        self.assertGreaterEqual(len(segs), 1)
        self.assertTrue(all(s.start is None for s in segs))


if __name__ == "__main__":
    unittest.main()
