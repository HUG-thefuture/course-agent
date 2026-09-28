"""HLS/直链下载：解析、加密识别、本地服务器端到端下载。"""

import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from course_agent.ingest.hls import (DownloadError, build_ffmpeg_args,
                                     download_direct, download_stream,
                                     parse_m3u8)
from course_agent.ingest.hls import _hls_python_download

MEDIA_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:9.0,
seg0.ts
#EXTINF:9.0,
seg1.ts
#EXTINF:9.0,
seg2.ts
#EXT-X-ENDLIST
"""

MASTER_M3U8 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720
media.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=480000,RESOLUTION=640x360
media_low.m3u8
"""

ENC_M3U8 = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x00000000000000000000000000000001
#EXTINF:9.0,
seg0.ts
#EXT-X-ENDLIST
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _LocalSite:
    """本地静态服务器，模拟回放 CDN。"""

    def __init__(self, root: Path):
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_QuietHandler, directory=str(root)))
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"


class TestParseM3u8(unittest.TestCase):
    def test_media_playlist(self):
        info = parse_m3u8(MEDIA_M3U8, "http://h/a/media.m3u8")
        self.assertEqual(info["kind"], "media")
        self.assertEqual(len(info["segments"]), 3)
        self.assertTrue(info["segments"][0].startswith("http://h/a/seg0.ts"))
        self.assertIsNone(info["key"])

    def test_master_playlist_picks_highest_bandwidth(self):
        info = parse_m3u8(MASTER_M3U8, "http://h/master.m3u8")
        self.assertEqual(info["kind"], "master")
        self.assertEqual(info["variants"][0], "http://h/media.m3u8")

    def test_encryption_detected(self):
        info = parse_m3u8(ENC_M3U8, "http://h/enc.m3u8")
        self.assertIsNotNone(info["key"])
        self.assertEqual(info["key"][0], "AES-128")
        self.assertEqual(info["key"][1], "http://h/enc.key")

    def test_no_segments_raises(self):
        with self.assertRaises(DownloadError):
            parse_m3u8("#EXTM3U\n#EXT-X-ENDLIST\n", "http://h/x.m3u8")

    def test_build_ffmpeg_args(self):
        args = build_ffmpeg_args("http://x/a.m3u8?t=1", Path("out.mp4"))
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("http://x/a.m3u8?t=1", args)
        self.assertIn("-c", args)
        self.assertTrue(args[-1].endswith("out.mp4"))


class TestDownload(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        (self.root / "media.m3u8").write_text(MEDIA_M3U8, encoding="utf-8")
        (self.root / "media_low.m3u8").write_text(MEDIA_M3U8, encoding="utf-8")
        (self.root / "master.m3u8").write_text(MASTER_M3U8, encoding="utf-8")
        (self.root / "enc.m3u8").write_text(ENC_M3U8, encoding="utf-8")
        (self.root / "enc.key").write_bytes(b"0123456789abcdef")
        self.seg_bytes = [bytes([65 + i]) * 100 for i in range(3)]
        for i, data in enumerate(self.seg_bytes):
            (self.root / f"seg{i}.ts").write_bytes(data)
        (self.root / "movie.mp4").write_bytes(b"MP4DATA" * 500)

    def tearDown(self):
        self._td.cleanup()

    def test_hls_python_download_ordered(self):
        with _LocalSite(self.root) as site:
            out = self.root / "out" / "video.ts"
            _hls_python_download(site.url("media.m3u8"), out)
            data = out.read_bytes()
            expected = b"".join(self.seg_bytes)
            self.assertEqual(data, expected)

    def test_hls_master_resolves_to_media(self):
        with _LocalSite(self.root) as site:
            out = self.root / "out" / "video2.ts"
            _hls_python_download(site.url("master.m3u8"), out)
            self.assertEqual(out.read_bytes(), b"".join(self.seg_bytes))

    def test_encrypted_without_pycryptodome(self):
        import importlib.util
        if importlib.util.find_spec("Crypto") is not None:
            self.skipTest("pycryptodome 已安装，跳过降级提示测试")
        with _LocalSite(self.root) as site:
            out = self.root / "out" / "enc.ts"
            with self.assertRaises(DownloadError) as ctx:
                _hls_python_download(site.url("enc.m3u8"), out)
            self.assertIn("pycryptodome", str(ctx.exception))

    def test_direct_download(self):
        with _LocalSite(self.root) as site:
            out = self.root / "out" / "movie.mp4"
            download_direct(site.url("movie.mp4"), out)
            self.assertEqual(out.stat().st_size, 7 * 500)

    def test_download_stream_cache(self):
        with _LocalSite(self.root) as site:
            out = self.root / "out" / "cached.mp4"
            out.write_bytes(b"already-here")
            # download_stream 有 ffmpeg 时会走 ffmpeg——用不存在的缓存直接短路
            download_stream(site.url("movie.mp4"), out)
            self.assertEqual(out.read_bytes(), b"already-here")


if __name__ == "__main__":
    unittest.main()
