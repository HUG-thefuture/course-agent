"""腾讯会议适配：链接识别、提取码合并、页面扫描、逐字稿提取、引导话术。"""

import json
import unittest

import course_agent.ingest.loader as loader_mod
from course_agent.ingest.loader import classify_url
from course_agent.ingest.tencent import (add_access_code, embedded_password,
                                         guided_steps, harvest_minutes,
                                         is_tencent_share, minutes_rows,
                                         try_extract_video_url, _short_code,
                                         parse_share_page, record_info)

SHARE = ("https://meeting.tencent.com/v2/cloud-record/share.html"
         "?record_id=abc123&access_code=8888")
SHARE_NO_CODE = ("https://meeting.tencent.com/v2/cloud-record/share.html"
                 "?record_id=abc123")


class TestShortCode(unittest.TestCase):
    """短码解析：真实链接后面常挂提取码与中文标题，不能把中文当短码。"""

    def test_plain(self):
        self.assertEqual(_short_code("https://meeting.tencent.com/crm/2ydzBry00f"),
                         "2ydzBry00f")
        self.assertEqual(_short_code("https://meeting.tencent.com/cw/2ydzBry00f?from=3"),
                         "2ydzBry00f")

    def test_with_embedded_password(self):
        self.assertEqual(
            _short_code("https://meeting.tencent.com/crm/2kqXE3Bn06/QCBC"), "2kqXE3Bn06")

    def test_with_password_and_chinese_title(self):
        url = "https://meeting.tencent.com/crm/2G6MVok995/JGCJ/大模型概述-RAG&微调&续训&智能体开发"
        self.assertEqual(_short_code(url), "2G6MVok995")

    def test_embedded_password(self):
        self.assertEqual(embedded_password(
            "https://meeting.tencent.com/crm/2kqXE3Bn06/QCBC"), "QCBC")
        self.assertEqual(embedded_password(
            "https://meeting.tencent.com/crm/2kqXE3Bn06/QCBC/大模型概述-LLM常识"), "QCBC")
        self.assertEqual(embedded_password(
            "https://meeting.tencent.com/crm/2ydzBry00f"), "")
        # 第三段是中文标题而非提取码时不能误判
        self.assertEqual(embedded_password(
            "https://meeting.tencent.com/crm/abc123/大模型概述"), "")


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


class TestSharePageParsing(unittest.TestCase):
    """分享页 serverData 解析（2026-11 起旧接口不可靠，改为直接读页面）。"""

    def _rsc_html(self, server_data: dict) -> str:
        """构造 Next.js RSC 风格的转义 serverData 片段。"""
        inner = json.dumps(server_data, ensure_ascii=False).replace('"', '\\"')
        return ('<script>self.__next_f.push([1,"18:{\\"data\\":{\\"query\\":'
                '{\\"serverData\\":' + inner + '},\\"foo\\":0}}]\\n")</script>')

    def test_parse_real_escaped_shape(self):
        """关键：转义 JSON 必须先还原引号再配对，否则会截断。"""
        d = {"code": 4033, "sharing_state": "1", "pwd": "IB7A",
             "encode_uni_record_id": "ENC123",
             "meeting_info": {"subject": "MS/langgraph-5qaL"},
             "recordings": [{"id": "2118151175657656321",
                             "duration": 5713536, "minutes_status": 1}]}
        import course_agent.ingest.tencent as t
        orig = t._http_get
        t._http_get = lambda *a, **k: self._rsc_html(d)
        try:
            got = parse_share_page("lvrdVLX420")
        finally:
            t._http_get = orig
        self.assertEqual(got.get("recording_id"), "2118151175657656321")
        self.assertEqual(got.get("pwd"), "IB7A")
        self.assertEqual(got.get("encode_uni_record_id"), "ENC123")
        self.assertEqual(got.get("minutes_status"), 1)
        self.assertAlmostEqual(got.get("duration_min"), 95.2256, places=2)
        self.assertEqual(got.get("recordings"), 1)

    def test_base64_subject_decoded(self):
        import base64
        import course_agent.ingest.tencent as t
        name = base64.b64encode("1、langgraph-概述".encode()).decode().rstrip("=")
        d = {"meeting_info": {"subject": name},
             "recordings": [{"id": "R1", "duration": 60000}]}
        orig = t._http_get
        t._http_get = lambda *a, **k: self._rsc_html(d)
        try:
            got = parse_share_page("x")
        finally:
            t._http_get = orig
        self.assertEqual(got.get("subject"), "1、langgraph-概述")

    def test_no_serverdata_returns_empty(self):
        import course_agent.ingest.tencent as t
        orig = t._http_get
        t._http_get = lambda *a, **k: "<html>普通页面</html>"
        try:
            self.assertEqual(parse_share_page("x"), {})
        finally:
            t._http_get = orig

    def test_record_info_uses_page_without_legacy_api(self):
        """页面有 recording_id 时不应再调用 get-multi-record-info。"""
        import course_agent.ingest.tencent as t
        d = {"pwd": "IB7A", "recordings": [{"id": "RPAGE", "duration": 3000000}]}
        calls = []

        def fake(url, **kw):
            calls.append(url)
            if "/cw/" in url:
                return self._rsc_html(d)
            raise AssertionError("不应再调用旧接口: " + url[:60])

        orig = t._http_get
        t._http_get = fake
        try:
            rid, minutes = record_info("uuid", "IB7A", short_code="lvrdVLX420")
        finally:
            t._http_get = orig
        self.assertEqual(rid, "RPAGE")
        self.assertEqual(minutes, 50.0)
        self.assertFalse(any("get-multi-record-info" in c for c in calls))

    def test_record_info_rejects_wrong_password_from_page(self):
        import course_agent.ingest.tencent as t
        d = {"pwd": "IB7A", "recordings": [{"id": "R1", "duration": 60000}]}
        orig = t._http_get
        t._http_get = lambda *a, **k: self._rsc_html(d)
        try:
            with self.assertRaises(ValueError) as cm:
                record_info("uuid", "WRONG", short_code="lvrdVLX420")
        finally:
            t._http_get = orig
        self.assertIn("提取码不正确", str(cm.exception))

    def test_record_info_requires_password_when_page_has_one(self):
        import course_agent.ingest.tencent as t
        d = {"pwd": "IB7A", "recordings": [{"id": "R1", "duration": 60000}]}
        orig = t._http_get
        t._http_get = lambda *a, **k: self._rsc_html(d)
        try:
            with self.assertRaises(ValueError) as cm:
                record_info("uuid", "", short_code="lvrdVLX420")
        finally:
            t._http_get = orig
        self.assertIn("需要提取码", str(cm.exception))


class TestHarvestMinutes(unittest.TestCase):
    """逐字稿自动提取链：_http_get 单点打桩（share_uuid/record_info/minutes 全走它）。"""

    import course_agent.ingest.tencent as _t

    def _stub(self, mapping):
        """mapping: URL 子串 -> 响应文本；命中第一个即返回。"""
        def fake(url, **kw):
            for key, body in mapping.items():
                if key in url:
                    return body
            raise AssertionError(f"未打桩的 URL: {url[:80]}")
        self._orig = self._t._http_get
        self._t._http_get = fake

    def _tearDown(self):
        self._t._http_get = self._orig

    def _ok_bodies(self):
        uuid = "9d1dac6c-8243-4122-9919-8d2dffae59f9"
        minutes = json.dumps({"code": 0, "minutes": {"paragraphs": [
            {"pid": 1, "start_time": 1500, "sentences": [
                {"words": [{"text": "大家好"}, {"text": "，开始上课"}]}]},
            {"pid": 2, "start_time": 62000, "sentences": [
                {"words": [{"text": "今天讲 FastAPI"}]}]},
            {"pid": 3, "start_time": 0, "sentences": [{"words": [{"text": "  "}]}]},
        ]}})
        return {
            "/cw/": f"<html>shareId={uuid} end</html>",
            "get-multi-record-info": json.dumps(
                {"code": 0, "data": {"base_infos": [
                    {"recording_id": "R123", "duration": "3600000"}]}}),
            "minutes/detail": minutes,
        }

    def test_happy_path(self):
        self._stub(self._ok_bodies())
        try:
            rows, meta = harvest_minutes(
                "https://meeting.tencent.com/crm/abcd1234", password="QSZI")
        finally:
            self._tearDown()
        self.assertEqual(len(rows), 2)              # 纯空白段被跳过
        self.assertEqual(rows[0]["text"], "大家好，开始上课")
        self.assertAlmostEqual(rows[0]["start"], 1.5)
        self.assertEqual(meta["recording_id"], "R123")
        self.assertEqual(meta["duration_min"], 60.0)

    def test_split_version_fallback(self):
        """split_version=1 空段落时应回退试 0（2026-08 底前旧记录）。"""
        bodies = self._ok_bodies()
        minutes_ok = bodies["minutes/detail"]

        # sv=1 返回空、sv=0 返回正常
        def fake2(url, **kw):
            for key, body in bodies.items():
                if key in url:
                    if key == "minutes/detail":
                        return (json.dumps({"code": 0, "minutes": {"paragraphs": []}})
                                if "split_version=1" in url else minutes_ok)
                    return body
            raise AssertionError(url[:60])
        orig = self._t._http_get
        self._t._http_get = fake2
        try:
            rows, meta = harvest_minutes("https://meeting.tencent.com/crm/x1")
        finally:
            self._t._http_get = orig
        self.assertEqual(len(rows), 2)

    def test_bad_password_readable_error(self):
        bodies = self._ok_bodies()
        bodies["get-multi-record-info"] = json.dumps(
            {"code": 0, "data": {"base_infos": []}})   # 密码错时接口返回空列表
        self._stub(bodies)
        try:
            with self.assertRaises(ValueError) as cm:
                harvest_minutes("https://meeting.tencent.com/crm/abcd", password="XXXX")
        finally:
            self._tearDown()
        self.assertIn("提取码", str(cm.exception))

    def test_no_minutes_readable_error(self):
        bodies = self._ok_bodies()
        bodies["minutes/detail"] = json.dumps({"code": 0, "minutes": {"paragraphs": []}})
        self._stub(bodies)
        try:
            with self.assertRaises(ValueError) as cm:
                harvest_minutes("https://meeting.tencent.com/crm/abcd")
        finally:
            self._tearDown()
        self.assertIn("逐字稿", str(cm.exception))

    def test_minutes_rows_pure(self):
        rows = minutes_rows({"minutes": {"paragraphs": [
            {"start_time": 0, "sentences": [{"words": [{"text": "hi"}]}]}]}})
        self.assertEqual(rows, [{"start": 0.0, "text": "hi"}])
        self.assertEqual(minutes_rows({}), [])


if __name__ == "__main__":
    unittest.main()
