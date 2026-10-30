"""GUI 启动器与构建指纹：确保改了代码就能生效（而不是旧进程一直服务）。"""

import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from course_agent import buildinfo


class TestBuildId(unittest.TestCase):
    def test_stable_and_short(self):
        a = buildinfo.build_id()
        b = buildinfo.build_id()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in a))

    def test_changes_with_source_mtime(self):
        """源码改动（mtime/size 变化）必须改变指纹，否则启动器不会重启旧进程。"""
        import tempfile
        import time

        td = tempfile.TemporaryDirectory()
        try:
            root = Path(td.name)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            first = buildinfo.build_id(root)
            time.sleep(0.01)
            (root / "a.py").write_text("x = 2\n", encoding="utf-8")
            self.assertNotEqual(first, buildinfo.build_id(root))
        finally:
            td.cleanup()


class _BuildHandler(BaseHTTPRequestHandler):
    payload = b'{"running": "old123", "disk": "new456", "stale": true}'

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


class TestLaunchGuiProbe(unittest.TestCase):
    """tools/launch_gui.py 的版本探测：判定旧实例并换端口启动的决策依据。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools import launch_gui
        self.lg = launch_gui

    def _serve(self, handler=_BuildHandler):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def test_probe_detects_stale_build(self):
        httpd = self._serve()
        try:
            info = self.lg.probe(httpd.server_address[1])
            self.assertIsNotNone(info)
            self.assertTrue(info["stale"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_probe_returns_none_without_instance(self):
        self.assertIsNone(self.lg.probe(9))

    def test_listen_pid_no_match(self):
        self.assertIsNone(self.lg.listen_pid(9))


if __name__ == "__main__":
    unittest.main()
