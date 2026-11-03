"""安装向导：路径推导、安装落盘、快捷方式与卸载的回归测试。

GUI 部分（tkinter）不在这里测——无头环境弹不出来；只测可复用的纯逻辑，
以及真正落盘/清理的部分（用临时目录模拟目标位置）。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent import installer as inst  # noqa: E402


class TestPaths(unittest.TestCase):
    def test_default_root_is_parent_not_app_dir(self):
        """默认位置返回父目录，安装时才拼 CourseAgent 子目录（避免双层嵌套）。"""
        root = inst.default_install_root()
        self.assertNotEqual(root.name, inst.APP_DIR_NAME)

    def test_desktop_and_startmenu_paths(self):
        self.assertTrue(str(inst.desktop_dir()).endswith("Desktop"))
        self.assertIn("Start Menu", str(inst.start_menu_dir()))

    def test_self_exe_requires_frozen(self):
        """源码运行时不该假装能安装（否则会把 python.exe 复制出去）。"""
        with mock.patch.object(inst.sys, "frozen", False, create=True):
            with self.assertRaises(RuntimeError):
                inst.self_exe()

    def test_self_exe_returns_exe_when_frozen(self):
        with mock.patch.object(inst.sys, "frozen", True, create=True), \
                mock.patch.object(inst.sys, "executable", r"C:\x\CourseAgent.exe"):
            self.assertEqual(str(inst.self_exe()), r"C:\x\CourseAgent.exe")


class TestInstallTo(unittest.TestCase):
    """用临时目录当"用户选择的安装位置"，验证文件真的落到位。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.fake_exe = self.root / "source" / "CourseAgent.exe"
        self.fake_exe.parent.mkdir(parents=True)
        self.fake_exe.write_bytes(b"FAKE-EXE")

    def tearDown(self):
        self._td.cleanup()

    def _install(self, **kw):
        target = self.root / "install_here"
        with mock.patch.object(inst, "self_exe", return_value=self.fake_exe), \
                mock.patch.object(inst, "_resource", return_value=None), \
                mock.patch.object(inst, "_make_shortcut") as mk:
            app_dir = inst.install_to(target, **kw)
        return app_dir, mk

    def test_installs_into_named_subdir(self):
        app_dir, _ = self._install(desktop=False, start_menu=False)
        self.assertEqual(app_dir, self.root / "install_here" / inst.APP_DIR_NAME)
        self.assertTrue((app_dir / inst.EXE_NAME).is_file())
        self.assertEqual((app_dir / inst.EXE_NAME).read_bytes(), b"FAKE-EXE")

    def test_creates_shortcuts_when_requested(self):
        _, mk = self._install(desktop=True, start_menu=True)
        self.assertEqual(mk.call_count, 2)     # 桌面 + 开始菜单

    def test_no_shortcuts_when_declined(self):
        _, mk = self._install(desktop=False, start_menu=False)
        self.assertEqual(mk.call_count, 0)

    def test_writes_uninstall_batch(self):
        app_dir, _ = self._install(desktop=False, start_menu=False)
        bat = app_dir / "uninstall.bat"
        self.assertTrue(bat.is_file())
        text = bat.read_text(encoding="utf-8")
        self.assertIn("rmdir", text)
        self.assertIn("确认卸载", text)

    def test_example_config_copied_when_resource_present(self):
        example = self.root / "cfg" / "config.example.json"
        example.parent.mkdir()
        example.write_text('{"prefer": "auto"}', encoding="utf-8")
        target = self.root / "inst2"
        with mock.patch.object(inst, "self_exe", return_value=self.fake_exe), \
                mock.patch.object(inst, "_resource", return_value=example), \
                mock.patch.object(inst, "_make_shortcut"):
            app_dir = inst.install_to(target, desktop=False, start_menu=False)
        self.assertTrue((app_dir / "config.example.json").is_file())

    def test_existing_config_not_overwritten(self):
        """重装/升级时不能覆盖用户已经填好的配置。"""
        example = self.root / "cfg" / "config.example.json"
        example.parent.mkdir()
        example.write_text('{"new": 1}', encoding="utf-8")
        target = self.root / "inst3"
        app_dir = target / inst.APP_DIR_NAME
        app_dir.mkdir(parents=True)
        user_cfg = app_dir / "config.example.json"
        user_cfg.write_text('{"user": "edited"}', encoding="utf-8")
        with mock.patch.object(inst, "self_exe", return_value=self.fake_exe), \
                mock.patch.object(inst, "_resource", return_value=example), \
                mock.patch.object(inst, "_make_shortcut"):
            inst.install_to(target, desktop=False, start_menu=False)
        self.assertEqual(user_cfg.read_text(encoding="utf-8"), '{"user": "edited"}')

    def test_reinstall_is_idempotent(self):
        self._install(desktop=False, start_menu=False)
        app_dir, _ = self._install(desktop=False, start_menu=False)
        self.assertTrue((app_dir / inst.EXE_NAME).is_file())


class TestUninstall(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.app = self.root / inst.APP_DIR_NAME
        self.app.mkdir()
        (self.app / "CourseAgent.exe").write_bytes(b"x")

    def tearDown(self):
        self._td.cleanup()

    def test_remove_deletes_shortcuts_but_keeps_user_data(self):
        desk = self.root / "Desktop"
        desk.mkdir()
        lnk = desk / inst.LNK_DESKTOP
        lnk.write_text("lnk", encoding="utf-8")
        (self.app / "data").mkdir()
        (self.app / "data" / "keep.json").write_text("{}", encoding="utf-8")

        with mock.patch.object(inst, "desktop_dir", return_value=desk), \
                mock.patch.object(inst, "start_menu_dir",
                                  return_value=self.root / "SM"):
            notes = inst.remove_install(self.root)

        self.assertFalse(lnk.exists())                       # 快捷方式已删
        self.assertTrue((self.app / "data" / "keep.json").is_file())  # 用户数据保留
        self.assertTrue(any("手动删除" in n for n in notes))


class TestMainDispatch(unittest.TestCase):
    def test_no_args_prints_help(self):
        with mock.patch.object(inst.argparse.ArgumentParser, "print_help"):
            self.assertEqual(inst.main(["prog"]), 1)

    def test_install_gui_const(self):
        with mock.patch.object(inst, "run_gui_install", return_value=7) as g:
            rc = inst.main(["prog", "--install"])
        self.assertEqual(rc, 7)
        g.assert_called_once()

    def test_install_with_dir(self):
        with mock.patch.object(inst, "run_gui_install", return_value=0) as g:
            inst.main(["prog", "--install", r"D:\tools"])
        self.assertEqual(g.call_args[0][0], Path(r"D:\tools"))

    def test_uninstall(self):
        with mock.patch.object(inst, "run_gui_uninstall", return_value=0) as u:
            self.assertEqual(inst.main(["prog", "--uninstall"]), 0)
        u.assert_called_once()

    def test_silent_install(self):
        with mock.patch.object(inst, "run_silent_install", return_value=0) as s:
            rc = inst.main(["prog", "--install", r"D:\x", "--silent"])
        self.assertEqual(rc, 0)
        self.assertEqual(s.call_args[0][0], Path(r"D:\x"))
        self.assertTrue(s.call_args[1]["shortcuts"])

    def test_silent_install_no_shortcuts(self):
        with mock.patch.object(inst, "run_silent_install", return_value=0) as s:
            inst.main(["prog", "--install", r"D:\x", "--silent", "--no-shortcuts"])
        self.assertFalse(s.call_args[1]["shortcuts"])

    def test_install_dir_without_silent_goes_gui(self):
        """给了目录但没 --silent：仍弹 GUI 让用户确认，不静默装。"""
        with mock.patch.object(inst, "run_gui_install", return_value=0) as g:
            inst.main(["prog", "--install", r"D:\x"])
        g.assert_called_once()


class TestSilentInstall(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(inst, "install_to",
                               return_value=Path(r"D:\x\CourseAgent")):
            self.assertEqual(inst.run_silent_install(Path(r"D:\x")), 0)

    def test_permission_denied_gives_actionable_message(self):
        with mock.patch.object(inst, "install_to",
                               side_effect=PermissionError("denied")), \
                mock.patch("builtins.print") as p:
            rc = inst.run_silent_install(Path(r"C:\Windows"))
        self.assertEqual(rc, 2)
        printed = " ".join(str(c) for c in p.call_args_list)
        self.assertIn("权限", printed)

    def test_generic_error(self):
        with mock.patch.object(inst, "install_to", side_effect=OSError("boom")):
            self.assertEqual(inst.run_silent_install(Path(r"D:\x")), 1)


if __name__ == "__main__":
    unittest.main()
