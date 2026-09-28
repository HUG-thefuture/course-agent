"""腾讯会议云录制回放适配器。

分享链接形如：
  https://meeting.tencent.com/v2/cloud-record/share.html?record_id=xxx&access_code=yyy

现实情况（2026-09 调研）：回放页是 JS 渲染的，播放流地址由页面用 record_id +
access_code 向腾讯接口换取，通常还带时效 token，服务端直接抓 HTML 往往拿不到。
因此本适配器采用两层策略：
  1. 自动尝试：抓分享页并正则扫描内嵌的 m3u8/mp4 地址（部分场景可用）；
  2. 引导抓取：自动失败时输出清晰的浏览器抓流步骤，用户把 m3u8/mp4 地址交给
     `python main.py run --input <流地址>`，后续下载/转写/整理全自动。
另外，若分享者勾选了"允许下载"，直接从回放页下载 mp4 后喂给 run 命令最简单可靠。

注意：仅处理你合法拥有访问权限的课程回放，下载请遵守分享者设置的权限。
"""

from __future__ import annotations

import logging
import re
import urllib.parse

_log = logging.getLogger(__name__)

_SHARE_MARKERS = ("meeting.tencent.com", "cloud-record")
_MEDIA_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s\"'<>\\]*)?")
# JS/JSON 里常见的转义先还原，避免漏检
_UNESCAPES = (("\\/", "/"), ("\\u0026", "&"), ("\\x26", "&"))


def is_tencent_share(url: str) -> bool:
    low = (url or "").lower()
    return all(m in low for m in _SHARE_MARKERS) or "share.html" in low and "meeting.tencent.com" in low


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
        "无法自动从腾讯会议分享页提取视频流（页面为 JS 渲染 + 时效 token，属正常情况）。\n"
        "请按以下步骤抓一次播放地址（约 1 分钟，拿到地址后一切自动）：\n"
        f"  1. 浏览器打开回放链接并输入提取码（确认能正常播放）: {share_url}\n"
        "  2. 按 F12 打开开发者工具 → Network（网络）标签 → 筛选框输入 m3u8\n"
        "  3. 刷新页面并播放回放，找到形如 xxx.m3u8?token=... 的请求\n"
        "  4. 右键该请求 → Copy → Copy link address（复制完整地址，注意有时效尽快使用）\n"
        "  5. 运行: python main.py run --input \"<刚才复制的m3u8地址>\" --title \"课程名\"\n"
        "备选路径：\n"
        "  A. 若分享者勾选了“允许下载”，直接在回放页下载 mp4，然后 run --input 文件.mp4\n"
        "  B. 若老师能导出会议转写文稿（商业版功能），run --input 文稿.txt 更轻量\n"
    )
