"""CLI 端到端：demo / run / improve / rate / export-sft / config。"""

import json
import tempfile
import unittest
from pathlib import Path

from course_agent import cli

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TXT = ROOT / "examples" / "sample_lecture.txt"
EXAMPLE_SRT = ROOT / "examples" / "sample_lecture.srt"


class TestCli(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.cfg_path = self.td / "config.json"
        self.cfg_path.write_text(json.dumps({
            "data_dir": str(self.td / "data"),
            "output_dir": str(self.td / "output"),
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_demo_offline(self):
        rc = cli.main(["demo", "--outdir", str(self.td / "out"),
                       "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)
        out = self.td / "out"
        for name in ("notes.md", "key_nodes.md", "exercises.md",
                     "review_report.md", "session.json"):
            self.assertTrue((out / name).is_file(), f"{name} 缺失")
        session = json.loads((out / "session.json").read_text(encoding="utf-8"))
        self.assertTrue(session["passed"])

    def test_run_mock_with_srt(self):
        rc = cli.main(["run", "--input", str(EXAMPLE_SRT), "--mock",
                       "--no-memory", "--outdir", str(self.td / "run1"),
                       "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.td / "run1" / "notes.md").is_file())
        # 字幕应有时间戳出现在笔记中
        notes = (self.td / "run1" / "notes.md").read_text(encoding="utf-8")
        self.assertIn("[", notes)

    def test_run_mock_with_txt(self):
        rc = cli.main(["run", "--input", str(EXAMPLE_TXT), "--mock",
                       "--no-memory", "--outdir", str(self.td / "run2"),
                       "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)

    def test_improve_and_export_sft(self):
        rc = cli.main(["improve", "--input", str(EXAMPLE_TXT), "--rounds", "2",
                       "--mock", "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)
        metrics = self.td / "data" / "metrics.jsonl"
        self.assertTrue(metrics.is_file())
        lines = [json.loads(x) for x in
                 metrics.read_text(encoding="utf-8").strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual([r["round"] for r in lines], [1, 2])

        out = self.td / "sft.jsonl"
        rc = cli.main(["export-sft", "--out", str(out),
                       "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)
        rows = [json.loads(x) for x in
                out.read_text(encoding="utf-8").strip().splitlines()]
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("instruction", rows[0])

    def test_rate(self):
        rc = cli.main(["rate", "--session", "sess-x", "--section", "第一节",
                       "--score", "5", "--note", "不错",
                       "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)
        rc = cli.main(["rate", "--session", "sess-x", "--score", "9",
                       "--config", str(self.cfg_path)])
        self.assertEqual(rc, 1)  # 非法分数

    def test_config_report(self):
        rc = cli.main(["config", "--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)

    def test_missing_input_file(self):
        rc = cli.main(["run", "--input", "不存在_9x.txt", "--mock",
                       "--no-memory", "--config", str(self.cfg_path)])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
