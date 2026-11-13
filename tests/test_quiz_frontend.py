"""前端作答回显与自动暂存的真实执行测试。

服务端存了进度不代表用户能看到——页面脚本必须真的把 progress 回填到输入框
并把作答变化提交回服务端。这里用最小 DOM 桩在 Node 里**真实执行**页面脚本
（不是字符串断言），覆盖：

- 首次进入无缓存时不凭空出现答案；
- 有缓存时单选/多选/判断/简答/编程全部回填，判分与解析面板回显；
- 作答变化触发 POST /api/progress，且只发 user（靠服务端合并保住分数）；
- 切章再切回，本章作答不丢。

脚本见 tests/quiz_restore_stub.js。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from course_agent.webui import INDEX_HTML

_STUB = Path(__file__).resolve().parent / "quiz_restore_stub.js"


def _page_script() -> str:
    m = re.search(r"<script>(.*)</script>", INDEX_HTML, re.S)
    assert m is not None, "页面缺少 <script> 块"
    return m.group(1)


class TestQuizFrontendRestore(unittest.TestCase):
    def test_page_restores_answers_and_autosaves(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("无 node，跳过前端脚本执行测试")
        tmpdir = Path(tempfile.mkdtemp())
        try:
            js = tmpdir / "page.js"
            out_json = tmpdir / "result.json"
            js.write_text(_page_script(), encoding="utf-8")
            env = dict(os.environ, QUIZ_STUB_OUT=str(out_json))
            r = subprocess.run([node, str(_STUB), str(js)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120, env=env)
            self.assertTrue(out_json.is_file(),
                            f"桩未产出结果文件：{(r.stdout or '')[-2000:]}{(r.stderr or '')[-2000:]}")
            data = json.loads(out_json.read_text(encoding="utf-8"))
            failed = [x for x in data["results"] if not x["ok"]]
            self.assertEqual(data["fail"], 0,
                             "存在失败断言：\n" + "\n".join(
                                 f"- {x['label']}  detail={x['detail']}" for x in failed))
            _ = r
            # 断言关键回归点确实被验证过（避免桩静默跳过）
            labels = [x["label"] for x in data["results"]]
            joined = "\n".join(labels)
            for key in ("单选A已勾选", "多选ABC已勾选", "判断题『对』已勾选",
                        "简答题文本已回填", "编程题代码已回填",
                        "判分结果已回显", "汇总已回显", "解析面板已回显",
                        "自动触发了 POST /api/progress", "暂存内容是新作答",
                        "回到第1章后简答仍在"):
                self.assertIn(key, joined, f"未验证到：{key}\n{joined}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_inline_js_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("无 node，跳过 JS 语法校验")
        tmpdir = Path(tempfile.mkdtemp())
        try:
            p = tmpdir / "page.js"
            p.write_text(_page_script(), encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)],
                               capture_output=True, text=True, encoding="utf-8",
                               timeout=60)
            self.assertEqual(r.returncode, 0, f"内嵌 JS 语法错误:\n{r.stderr}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
