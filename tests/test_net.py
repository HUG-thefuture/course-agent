"""net 模块：直连优先、代理回落。"""

import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from course_agent import net
from course_agent.net import direct_opener, install_direct_opener, open_direct


class _MemHandler(BaseHTTPRequestHandler):
    """纯内存应答：GET 任意路径都返回固定文本，不读写磁盘（兼容沙箱）。"""

    def do_GET(self):
        body = "你好-direct".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MemHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class TestDirectOpener(unittest.TestCase):
    def setUp(self):
        # 每个用例都从未知状态开始（模块级「直连已坏」标记复位）
        net._DIRECT_BROKEN_HOSTS.clear()

    def tearDown(self):
        net._DIRECT_BROKEN_HOSTS.clear()

    def test_opener_is_singleton(self):
        self.assertIs(direct_opener(), direct_opener())

    def test_opener_bypasses_dead_system_proxy(self):
        """核心回归：系统里残留失效代理时，直连 opener 仍能取到内容。

        这是 GUI 报 `[WinError 10061] 由于目标计算机积极拒绝` 的根因——
        urlopen 的默认 opener 会在首次使用时把当时的系统代理固化下来。
        """
        httpd = _serve()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/x")
            with mock.patch.object(
                    urllib.request, "getproxies",
                    return_value={"http": "http://127.0.0.1:9",
                                  "https": "http://127.0.0.1:9"}):
                with direct_opener().open(req, timeout=5) as r:
                    self.assertEqual(r.read().decode("utf-8"), "你好-direct")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_install_direct_opener(self):
        install_direct_opener()
        self.assertIs(urllib.request._opener, direct_opener())

    def test_open_direct_local_http(self):
        httpd = _serve()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/y")
            with open_direct(req, timeout=5) as r:
                self.assertEqual(r.read().decode("utf-8"), "你好-direct")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_fallback_to_system_proxy_when_direct_fails(self):
        """直连被拒时应自动回退系统代理，而不是直接失败。"""
        httpd = _serve()
        port = httpd.server_address[1]
        calls = []

        class _FakeOpener:
            def open(self, req, timeout=None):
                calls.append("direct")
                raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        class _ProxyOpener:
            def open(self, req, timeout=None):
                calls.append("proxy")
                return urllib.request.build_opener().open(req, timeout=timeout)

        real_build = urllib.request.build_opener

        def fake_build(*handlers):
            if handlers and isinstance(handlers[0], urllib.request.ProxyHandler):
                return _ProxyOpener()
            return real_build(*handlers)

        try:
            with mock.patch.object(net, "direct_opener", return_value=_FakeOpener()), \
                    mock.patch.object(net, "_is_loopback", return_value=False), \
                    mock.patch.object(urllib.request, "build_opener", fake_build):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/z")
                with open_direct(req, timeout=5) as r:
                    self.assertEqual(r.read().decode("utf-8"), "你好-direct")
            self.assertEqual(calls, ["direct", "proxy"])
            # 回退成功时记住该主机"直连已坏"，后续请求不再每次都白等一次连接超时
            self.assertIn("127.0.0.1", net._DIRECT_BROKEN_HOSTS)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_loopback_never_uses_proxy(self):
        """本机地址永不走代理：系统代理配错时也不能连不上本地 Ollama / GUI。"""
        httpd = _serve()
        port = httpd.server_address[1]
        calls = []

        class _FakeOpener:
            def open(self, req, timeout=None):
                calls.append("direct")
                raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        try:
            with mock.patch.object(net, "direct_opener", return_value=_FakeOpener()):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/a")
                with self.assertRaises(urllib.error.URLError):
                    open_direct(req, timeout=2)
            self.assertEqual(calls, ["direct"])           # 没有回退代理
            self.assertEqual(net._DIRECT_BROKEN_HOSTS, set())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_remote_host_tracks_separately_from_local(self):
        """本地服务连不上，不应把外网主机的直连也标记为坏。"""
        net._DIRECT_BROKEN_HOSTS.add("127.0.0.1")
        self.assertNotIn("meeting.tencent.com", net._DIRECT_BROKEN_HOSTS)

    def test_http_error_not_masked_by_fallback(self):
        """服务端 4xx/5xx 是有效应答，不应被当成网络故障去回落代理。"""
        def boom(req, timeout=None):
            raise urllib.error.HTTPError("http://x", 404, "nf", None, None)

        class _FakeOpener:
            open = staticmethod(boom)

        with mock.patch.object(net, "direct_opener", return_value=_FakeOpener()):
            req = urllib.request.Request("http://x/404")
            with self.assertRaises(urllib.error.HTTPError):
                open_direct(req, timeout=1)


if __name__ == "__main__":
    unittest.main()
