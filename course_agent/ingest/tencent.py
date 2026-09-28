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

import logging
import re
import urllib.parse

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


def guided_steps(share_url: str) -> str:
    return (
        "无法从该腾讯会议链接自动提取数据（页面 JS 渲染 + 网关签名，属正常情况）。\n"
        f"回放链接: {share_url}\n"
        "请在浏览器打开链接并输入提取码，确认能正常播放后，二选一：\n"
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
