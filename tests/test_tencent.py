"""腾讯会议适配：链接识别、提取码合并、页面扫描、引导话术。"""

import unittest

import course_agent.ingest.loader as loader_mod
from course_agent.ingest.loader import classify_url
from course_agent.ingest.tencent import (add_access_code, guided_steps,
                                         is_tencent_share,
                                         try_extract_video_url)

SHARE = ("https://meeting.tencent.com/v2/cloud-record/share.html"
         "?record_id=abc123&access_code=8888")
SHARE_NO_CODE = ("https://meeting.tencent.com/v2/cloud-record/share.html"
                 "?record_id=abc123")


class TestDetect(unittest.TestCase):
    def test_share_link(self):
        self.assertTrue(is_tencent_share(SHARE))
        self.assertTrue(is_tencent_share(SHARE_NO_CODE))
        self.assertTrue(is_tencent_share("https://meeting.tencent.com/crm/2ydzBry00f"))
        self.assertTrue(is_tencent_share("https://meeting.tencent.com/cw/2ydzBry00f"))

    def test_non_share(self):
        self.assertFalse(is_tencent_share("https://example.com/vod/a.m3u8"))
        self.assertFalse(is_tencent_share("https://meeting.tencent.com/help"))
        self.assertFalse(is_tencent_share("https://meeting.tencent.com/download"))

    def test_classify_url(self):
        self.assertEqual(classify_url(SHARE), "tencent_share")
        self.assertEqual(classify_url("https://cdn.x.com/live/index.m3u8?token=1"),
                         "media")
        self.assertEqual(classify_url("https://cdn.x.com/vod/a.mp4"), "media")
        self.assertEqual(classify_url("https://example.com/course/page"), "page")


class TestAccessCode(unittest.TestCase):
    def test_add_code(self):
        out = add_access_code(SHARE_NO_CODE, "9527")
        self.assertIn("access_code=9527", out)
        self.assertIn("record_id=abc123", out)

    def test_no_duplicate(self):
        self.assertEqual(add_access_code(SHARE, "9527"), SHARE)

    def test_empty_password_noop(self):
        self.assertEqual(add_access_code(SHARE_NO_CODE, ""), SHARE_NO_CODE)


class TestExtract(unittest.TestCase):
    def _patch_fetch(self, html):
        orig = loader_mod.fetch_url
        loader_mod.fetch_url = lambda *a, **k: ("页面标题", html)
        return orig

    def test_extract_escaped_m3u8(self):
        html = ('<html><body><script>var cfg = {"playUrl":'
                '"https:\\/\\/cdn.tencent.com\\/replay\\/main.m3u8?token=ab\\u0026exp=1"};'
                "</script></body></html>")
        orig = self._patch_fetch(html)
        try:
            got = try_extract_video_url(SHARE_NO_CODE, password="8888")
        finally:
            loader_mod.fetch_url = orig
        self.assertIsNotNone(got)
        self.assertIn("main.m3u8", got)
        self.assertNotIn("\\/", got)
        # 提取码应已并入请求 URL
        # （fetch 被打桩，验证 add_access_code 已在上游生效即可）

    def test_extract_prefers_m3u8_over_mp4(self):
        html = ('<body><a href="https://cdn.x.com/a.mp4">a</a>'
                "<script>u='https://cdn.x.com/b.m3u8?t=1'</script></body>")
        orig = self._patch_fetch(html)
        try:
            got = try_extract_video_url(SHARE)
        finally:
            loader_mod.fetch_url = orig
        self.assertIn(".m3u8", got)

    def test_no_media_returns_none(self):
        orig = self._patch_fetch("<html><body>普通页面，没有视频地址</body></html>")
        try:
            self.assertIsNone(try_extract_video_url(SHARE))
        finally:
            loader_mod.fetch_url = orig

    def test_fetch_failure_returns_none(self):
        orig = loader_mod.fetch_url
        def _boom(*a, **k):
            raise RuntimeError("403")
        loader_mod.fetch_url = _boom
        try:
            self.assertIsNone(try_extract_video_url(SHARE))
        finally:
            loader_mod.fetch_url = orig

    def test_guided_steps(self):
        text = guided_steps(SHARE)
        self.assertIn("F12", text)
        self.assertIn("minutes/detail", text)
        self.assertIn("mp4", text)
        self.assertIn("允许下载", text)
        self.assertIn(SHARE, text)


if __name__ == "__main__":
    unittest.main()
