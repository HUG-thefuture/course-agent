"""关键帧提取（真实 ffmpeg 集成）与视觉描述模块。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from course_agent.ingest.frames import FrameError, extract_keyframes
from course_agent.llm import MockLLM
from course_agent.llm.vision import captions_for_range, describe_frames
from course_agent.pipeline.models import Frame

FFMPEG = shutil.which("ffmpeg")


def _make_test_video(path: Path, duration: int = 5) -> None:
    """生成测试视频：两段不同画面拼接，保证有真实场景切换。"""
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"smptebars=duration={duration}:size=160x90:rate=5",
         "-f", "lavfi", "-i", f"testsrc2=duration={duration}:size=160x90:rate=5",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
         "-map", "[v]", str(path)],
        check=True, capture_output=True, timeout=120)


@unittest.skipIf(FFMPEG is None, "本机无 ffmpeg")
class TestExtractKeyframes(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.video = self.td / "lesson.mp4"
        _make_test_video(self.video)

    def tearDown(self):
        self._td.cleanup()

    def test_scene_detection_or_fallback(self):
        frames = extract_keyframes(self.video, self.td / "frames",
                                   max_frames=10, scene_threshold=0.2)
        self.assertTrue(frames)
        self.assertLessEqual(len(frames), 10)
        for f in frames:
            self.assertTrue(Path(f.path).is_file())
            self.assertGreaterEqual(f.time, 0.0)
        times = [f.time for f in frames]
        self.assertEqual(times, sorted(times))

    def test_fallback_sampling_when_no_scene_change(self):
        # 阈值提高到几乎不可能触发场景检测 → 走 60s 间隔采样兜底
        frames = extract_keyframes(self.video, self.td / "frames2",
                                   max_frames=3, scene_threshold=0.99)
        self.assertTrue(frames)
        self.assertLessEqual(len(frames), 3)

    def test_cap_max_frames(self):
        frames = extract_keyframes(self.video, self.td / "frames3",
                                   max_frames=2, scene_threshold=0.05)
        self.assertLessEqual(len(frames), 2)

    def test_missing_video(self):
        with self.assertRaises(FrameError):
            extract_keyframes(self.td / "nope.mp4", self.td / "f")


class TestDescribeFrames(unittest.TestCase):
    def test_captions_filled(self):
        frames = [Frame(path=f"/f{i}.jpg", time=float(i * 10)) for i in range(3)]
        out = describe_frames(MockLLM(), frames)
        self.assertTrue(all(f.caption for f in out))

    def test_bad_output_leaves_caption_empty(self):
        class _Bad:
            def chat_with_image(self, *a, **k):
                return "不是JSON"

        frames = [Frame(path="/x.jpg", time=1.0)]
        out = describe_frames(_Bad(), frames)
        self.assertEqual(out[0].caption, "")

    def test_captions_for_range_filter(self):
        frames = [
            Frame(path="a", time=10.0, caption="帧A"),
            Frame(path="b", time=50.0, caption="帧B"),
            Frame(path="c", time=200.0, caption="帧C"),
        ]
        got = captions_for_range(frames, start=0.0, end=100.0)
        self.assertIn("帧A", got)
        self.assertIn("帧B", got)
        self.assertNotIn("帧C", got)
        self.assertIn("屏幕演示记录", got)
        # 无帧/无描述 → 空串
        self.assertEqual(captions_for_range([], 0, 100), "")
        self.assertEqual(captions_for_range(
            [Frame(path="d", time=5.0)], 0, 100), "")


class TestChatWithImageTransport(unittest.TestCase):
    def test_image_payload_shape(self):
        from course_agent.llm.openai_compat import OpenAICompatClient
        captured = {}

        def transport(req, timeout):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return 200, json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode()

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "f.jpg"
            img.write_bytes(b"\xff\xd8fakejpeg")
            c = OpenAICompatClient("http://x/v1", "vl-model",
                                   transport=transport)
            out = c.chat_with_image("sys", "user text", [img])
        self.assertEqual(out, "ok")
        content = captured["body"]["messages"][1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"))

    def test_missing_image_raises(self):
        from course_agent.llm.base import LLMError
        from course_agent.llm.openai_compat import OpenAICompatClient
        c = OpenAICompatClient("http://x/v1", "m",
                               transport=lambda req, t: (200, b"{}"))
        with self.assertRaises(LLMError):
            c.chat_with_image("s", "u", ["不存在.jpg"])


import json  # noqa: E402  (置于文件尾供上方用例使用)

if __name__ == "__main__":
    unittest.main()
