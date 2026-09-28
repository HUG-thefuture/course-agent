"""腾讯会议逐字稿转换器测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from convert_tencent_minutes import convert  # noqa: E402

MINUTES = {
    "code": 0,
    "minutes": {
        "lang": "zh",
        "paragraphs": [
            {"pid": "0", "start_time": 753, "end_time": 104392,
             "sentences": [
                 {"sid": "0", "start_time": 753, "end_time": 3212,
                  "words": [{"wid": "0", "text": "给大家去讲一讲！"}]},
                 {"sid": "1", "start_time": 4013, "end_time": 6952,
                  "words": [{"wid": "1", "text": "我看以前的网站还能不能访问？"}]},
             ]},
            {"pid": "1", "start_time": 104500, "end_time": 200000,
             "sentences": [
                 {"sid": "2", "start_time": 104600, "end_time": 110000,
                  "words": [{"wid": "2", "text": "组件化开发是重点。"}]},
             ]},
            {"pid": "2", "start_time": 200000, "end_time": 210000,
             "sentences": []},  # 空段应被跳过
        ],
    },
}


class TestConvert(unittest.TestCase):
    def test_convert(self):
        rows = convert(MINUTES)
        self.assertEqual(len(rows), 2)  # 空段被过滤
        self.assertEqual(rows[0]["start"], 0.75)
        self.assertEqual(rows[0]["text"], "给大家去讲一讲！我看以前的网站还能不能访问？")
        self.assertEqual(rows[1]["start"], 104.5)

    def test_convert_top_level_paragraphs(self):
        rows = convert({"paragraphs": MINUTES["minutes"]["paragraphs"]})
        self.assertEqual(len(rows), 2)

    def test_empty_raises(self):
        with self.assertRaises(SystemExit):
            convert({"minutes": {"paragraphs": []}})

    def test_cli_roundtrip(self):
        import convert_tencent_minutes as m
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "minutes.json"
            dst = Path(td) / "transcript.json"
            src.write_text(json.dumps(MINUTES, ensure_ascii=False), encoding="utf-8")
            rc = m.main(["prog", str(src), str(dst)])
            self.assertEqual(rc, 0)
            rows = json.loads(dst.read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
