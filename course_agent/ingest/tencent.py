"""腾讯会议云录制回放适配器。

分享链接形态（2026-09 实测）：
  短链:   https://meeting.tencent.com/crm/2ydzBry00f
  移动页: https://meeting.tencent.com/cw/2ydzBry00f   （短链 JS 跳转目标）
  PC 页:  https://meeting.tencent.com/v2/cloud-record/share.html?record_id=xxx

实测结论：回放页 JS 渲染 + 网关签名，纯服务端抓 HTML 拿不到播放数据。两条可用路径：
  A. 逐字稿路径（推荐，无需转写）：回放页含 AI 纪要，F12 -> Network 筛选
     `minutes/detail`，把返回 JSON 交给 tools/convert_tencent_minutes.py
     转成管线输入（自带时间戳，精确到句）；
  B. 视频路径：Network 筛选 `mp4`（speaker=摄像头 / screen=屏幕共享两路），
     复制带 token 的 mp4 地址交给 `run`（ffmpeg 下载后本地转写）。
页面密码（提取码）在页面内输入，鉴权后接口与媒体地址才可见。

注意：仅处理你合法拥有访问权限的课程回放，下载请遵守分享者设置的权限。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request

_log = logging.getLogger(__name__)

_SHARE_HOST = "meeting.tencent.com"
_MEDIA_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s\"'<>\\]*)?")
# JS/JSON 里常见的转义先还原，避免漏检
_UNESCAPES = (("\\/", "/"), ("\\u0026", "&"), ("\\x26", "&"))


def is_tencent_share(url: str) -> bool:
    low = (url or "").lower()
    if _SHARE_HOST not in low:
        return False
    return any(m in low for m in (
        "/crm/", "/cw/", "/cloud-record", "share.html"))


def add_access_code(url: str, password: str) -> str:
    """把提取码并入 access_code 查询参数（已存在则不动）。"""
    if not password:
        return url
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    code_key = next((k for k in query if k.lower() in ("access_code", "accesscode")), None)
    if code_key:
        return url
    query.setdefault("access_code", password)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def try_extract_video_url(share_url: str, password: str = "",
                          timeout: int = 30) -> str | None:
    """尽力从分享页提取直连媒体地址；拿不到返回 None（走引导流程）。"""
    from .loader import fetch_url  # 惰性导入，避免循环依赖
    target = add_access_code(share_url, password)
    try:
        title, html = fetch_url(target, timeout=timeout)
    except Exception as e:
        _log.warning("分享页抓取失败（可能需要登录或提取码校验）: %s", e)
        return None
    for old, new in _UNESCAPES:
        html = html.replace(old, new)
    candidates = _MEDIA_RE.findall(html)
    if candidates:
        # 优先 m3u8（通常是完整回放），其次 mp4
        candidates.sort(key=lambda u: 0 if ".m3u8" in u else 1)
        _log.info("从分享页提取到媒体地址（%d 个候选）", len(candidates))
        return candidates[0]
    _log.info("分享页未内嵌媒体地址（JS 渲染页，属正常情况）")
    return None


# ---------------------------------------------------------------- 逐字稿 API 链
# 2026-09-28 起用于 123 门课全量采集的纯 HTTP 接口链（无浏览器/Cookie）：
#   短链 -> 页面内嵌 share UUID -> get-multi-record-info(带提取码) -> minutes/detail
# 有提取码的分享也必须传对 pwd，否则 record-info 返回空列表（页面表现为要求输入密码）。

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UA = {"User-Agent": "Mozilla/5.0 (CourseAgent)"}


def _http_get(url: str, timeout: int = 30, retries: int = 2) -> str:
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise OSError(f"请求失败: {last}")


def _short_code(share_url: str) -> str:
    path = urllib.parse.urlparse(share_url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return urllib.parse.unquote(tail).split("?")[0]


def share_uuid(share_url: str) -> str:
    """短链 -> 录制 share UUID（从 /cw/ 移动页 HTML 内嵌字段提取）。"""
    short = _short_code(share_url)
    if not short:
        raise ValueError("链接中未识别出分享短码")
    html = _http_get(f"https://meeting.tencent.com/cw/{short}")
    m = _UUID_RE.search(html)
    if not m:
        raise ValueError("页面未内嵌 share id（可能不是录制分享页或已失效）")
    return m.group(0)


def record_info(share: str, pwd: str) -> tuple[str, float]:
    """share UUID + 提取码 -> (recording_id, 时长分钟)。"""
    ts = int(time.time() * 1000)
    body = _http_get(
        "https://meeting.tencent.com/wemeet-tapi/v2/meetlog/public/record-detail/"
        f"get-multi-record-info?c_timestamp={ts}&c_nonce=h{ts % 100000}"
        "&platform=Web&c_app_uid=&c_district=0&c_lang=zh-CN&mock=1"
        f"&pwd={pwd}&auth_share_id={share}&uni_record_share_id={share}&activity_uid=")
    data = json.loads(body)
    if data.get("code") != 0:
        raise ValueError(f"record-info 接口错误 code={data.get('code')} {data.get('msg', '')}")
    base = (data.get("data") or {}).get("base_infos") or []
    if not base:
        raise ValueError("无录制文件或提取码不正确")
    return base[0]["recording_id"], int(base[0].get("duration", 0)) / 60000.0


def minutes_detail(share: str, pwd: str, recording_id: str,
                   split_version: int = 1) -> dict:
    ts = int(time.time() * 1000)
    body = _http_get(
        "https://meeting.tencent.com/wemeet-cloudrecording-webapi/v1/minutes/detail"
        f"?c_timestamp={ts}&c_nonce=t{ts % 100000}&platform=Web&c_app_uid=&c_district=0"
        f"&mock=1&id={share}&pwd={pwd}&activity_uid=&page_source=record"
        f"&meeting_id=&recording_id={recording_id}"
        "&lang=zh&minutes_version=0&limit=1000&return_ori=0&start_pid=0"
        f"&return_ori_minutes_translating=1&split_version={split_version}&fview=0")
    return json.loads(body)


def minutes_rows(data: dict) -> list[dict]:
    """minutes/detail 响应 -> [{start: 秒, text}]（CourseAgent 转写 json 格式）。"""
    rows: list[dict] = []
    for p in (data.get("minutes") or {}).get("paragraphs") or []:
        text = "".join(w.get("text", "")
                       for s in p.get("sentences", []) for w in s.get("words", []))
        if text.strip():
            rows.append({"start": round((p.get("start_time") or 0) / 1000.0, 2),
                         "text": text})
    return rows


def harvest_minutes(share_url: str, password: str = "") -> tuple[list[dict], dict]:
    """自动提取回放逐字稿。返回 (转写行, 元信息)。失败抛 ValueError（可读原因）。

    新版记录只有 split_version=1，2026-08 底前的部分记录只有 0——两者都试。
    """
    share = share_uuid(share_url)
    recording_id, duration_min = record_info(share, password)
    data: dict = {}
    rows: list[dict] = []
    err = ""
    for sv in (1, 0):
        data = minutes_detail(share, password, recording_id, split_version=sv)
        rows = minutes_rows(data)
        if data.get("code") == 0 and rows:
            break
        err = f"minutes 接口 code={data.get('code')} {data.get('msg', '')}"
    if not rows:
        if "无录制文件" in str(err) or data.get("code") != 0:
            raise ValueError(err or "无逐字稿数据")
        raise ValueError("该录制没有 AI 逐字稿（可能未生成或录制过短）")
    meta = {"recording_id": recording_id, "duration_min": round(duration_min, 1),
            "source": share_url}
    return rows, meta


def guided_steps(share_url: str, reason: str = "") -> str:
    return (
        "无法从该腾讯会议链接自动提取逐字稿"
        + (f"（{reason}）" if reason else "")
        + "。\n"
        + f"回放链接: {share_url}\n"
        "若该分享设有提取码，请在「提取码」栏填写后重试；若仍有问题，"
        "可在浏览器打开链接输入提取码播放后，按下面手工路径二选一：\n"
        "\n【路径 A · 逐字稿（推荐，无需下载视频、自带精确时间戳）】\n"
        "  1. F12 打开开发者工具 -> Network（网络）标签 -> 筛选框输入 minutes/detail\n"
        "  2. 点击页面上方「纪要/逐字稿」，右键该请求 -> Copy -> Copy link address\n"
        "  3. 把地址里的 limit=20 改成 limit=1000，浏览器直接访问，全选保存为 minutes.json\n"
        "  4. 运行:\n"
        "     python tools/convert_tencent_minutes.py minutes.json transcript.json\n"
        "     python main.py run --input transcript.json --title \"课程名\"\n"
        "\n【路径 B · 视频（截图演示类课程用）】\n"
        "  1. Network 筛选框输入 mp4，找到 recording-1_screen.mp4（屏幕共享）\n"
        "     或 recording-1_speaker.mp4（摄像头），右键 Copy link address\n"
        "  2. 尽快运行（token 有时效）:\n"
        "     python main.py run --input \"<复制的mp4地址>\" --title \"课程名\"\n"
        "     或只下载转写: python main.py fetch --input \"<地址>\"\n"
        "备选：若分享者勾选了「允许下载」，直接在回放页下载 mp4 再 run。\n"
    )
