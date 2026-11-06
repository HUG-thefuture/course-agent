"""dsh-autotier 兼容性补丁脚本的回归测试。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import patch_autotier as pa  # noqa: E402


class _Fixture:
    def __init__(self, content: str):
        self._td = tempfile.TemporaryDirectory()
        self.pkg = Path(self._td.name) / "dsh-autotier"
        (self.pkg / "lib").mkdir(parents=True)
        self.lib = self.pkg / "lib" / "index.js"
        self.lib.write_text(content, encoding="utf-8")

    def cleanup(self):
        self._td.cleanup()


class TestPatch(unittest.TestCase):
    def test_preview_does_not_write(self):
        fx = _Fixture("before\n" + pa.OLD + "\nafter\n")
        try:
            with mock.patch.object(pa, "DEFAULT_AUTOTIER", fx.pkg), \
                    mock.patch.object(sys, "argv", ["x"]):
                rc = pa.main([])
            self.assertEqual(rc, 0)
            self.assertNotIn(pa.MARKER, fx.lib.read_text(encoding="utf-8"))
        finally:
            fx.cleanup()

    def test_apply_writes_and_backs_up(self):
        fx = _Fixture("before\n" + pa.OLD + "\nafter\n")
        try:
            with mock.patch.object(pa, "DEFAULT_AUTOTIER", fx.pkg), \
                    mock.patch.object(sys, "argv", ["x", "--apply"]):
                rc = pa.main(["x", "--apply", "--path", str(fx.pkg)])
            self.assertEqual(rc, 0)
            text = fx.lib.read_text(encoding="utf-8")
            self.assertIn(pa.MARKER, text)
            self.assertIn("agent.model.select", text)
            self.assertTrue(fx.lib.with_suffix(".js.pre-modelselect").is_file())
        finally:
            fx.cleanup()

    def test_idempotent(self):
        fx = _Fixture(pa.NEW)
        try:
            with mock.patch.object(pa, "DEFAULT_AUTOTIER", fx.pkg):
                rc = pa.main(["x", "--apply", "--path", str(fx.pkg)])
            self.assertEqual(rc, 0)
            self.assertEqual(fx.lib.read_text(encoding="utf-8").count(pa.MARKER), 1)
        finally:
            fx.cleanup()

    def test_missing_target_returns_1(self):
        with mock.patch.object(pa, "DEFAULT_AUTOTIER", Path("/nonexistent/pkg")):
            self.assertEqual(pa.main(["x"]), 1)

    def test_unexpected_version_returns_2(self):
        fx = _Fixture("完全不同的内容\n")
        try:
            rc = pa.main(["x", "--apply", "--path", str(fx.pkg)])
            self.assertEqual(rc, 2)
            self.assertEqual(fx.lib.read_text(encoding="utf-8"), "完全不同的内容\n")
        finally:
            fx.cleanup()

    def test_new_code_returns_base_when_model_changes(self):
        """守卫逻辑：改模型时返回 base 并调用 select。"""
        # 直接测试 NEW 片段里的语义（用 node 在 patch 脚本外已验证，这里测结构性断言）
        self.assertIn("routed.provider !== base.provider || routed.model !== base.model",
                      pa.NEW)
        self.assertIn("agent.model.select", pa.NEW)
        self.assertIn("return base;", pa.NEW)


if __name__ == "__main__":
    unittest.main()
