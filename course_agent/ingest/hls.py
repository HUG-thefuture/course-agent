"""视频流下载：优先 ffmpeg（最稳，支持加密 HLS），无 ffmpeg 时用纯标准库 HLS 下载。

内存安全：所有下载都流式写盘（固定大小分块/滑动窗口），不把视频整体读进内存。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..net import open_direct

_log = logging.getLogger(__name__)

_CHUNK = 256 * 1024
_UA = {"User-Agent": "Mozilla/5.0 (CourseAgent/1.0)"}


class DownloadError(Exception):
    pass


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=_UA, method="GET")
    try:
        with open_direct(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise DownloadError(f"请求失败 {url}: {e}") from e


def build_ffmpeg_args(url: str, out_path: Path) -> list[str]:
    """ffmpeg 命令参数（独立成函数便于测试与审计）。-c copy 直接封装不重编码。"""
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-user_agent", _UA["User-Agent"],
        "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out_path),
    ]


def _ffmpeg_download(url: str, out_path: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise DownloadError("未找到 ffmpeg，无法用 ffmpeg 下载")
    cmd = build_ffmpeg_args(url, out_path)
    _log.info("ffmpeg 开始下载: %s", url[:100])
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")

    def _pump() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            _log.debug("ffmpeg: %s", line.strip())

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    start = time.time()
    try:
        while proc.poll() is None:
            time.sleep(15)
            if out_path.exists():
                _log.info("下载中… 已写盘 %.1f MB（%.0f 秒）",
                          out_path.stat().st_size / 1e6, time.time() - start)
        if proc.returncode != 0:
            raise DownloadError(f"ffmpeg 退出码 {proc.returncode}，下载失败")
    finally:
        t.join(timeout=2)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise DownloadError("ffmpeg 未产生有效输出文件")
    _log.info("下载完成: %s（%.1f MB，%.0f 秒）", out_path.name,
              out_path.stat().st_size / 1e6, time.time() - start)
    return out_path


def download_direct(url: str, out_path: Path) -> Path:
    """直链（mp4 等）流式下载：固定 256KB 分块写盘。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=_UA, method="GET")
    _log.info("直链下载: %s", url[:100])
    try:
        with open_direct(req, timeout=60) as resp, \
                out_path.open("wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        out_path.unlink(missing_ok=True)
        raise DownloadError(f"直链下载失败 {url}: {e}") from e
    if out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise DownloadError("下载内容为空")
    _log.info("下载完成: %s（%.1f MB）", out_path.name,
              out_path.stat().st_size / 1e6)
    return out_path


def parse_m3u8(text: str, base_url: str) -> dict:
    """解析 m3u8。返回 {"kind": "master"|"media", "variants": [url]} 或
    {"kind": "media", "segments": [url], "key": (method, key_url, iv)|None}。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        variants = []
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                m = _attr(ln, "BANDWIDTH")
                # 变体地址是紧跟的下一个非注释行
                for nxt in lines[i + 1:]:
                    if not nxt.startswith("#"):
                        variants.append((int(m) if m else 0,
                                         urllib.parse.urljoin(base_url, nxt)))
                        break
        variants.sort(reverse=True)
        if not variants:
            raise DownloadError("master m3u8 中没有可用的子播放列表")
        return {"kind": "master", "variants": [u for _, u in variants]}
    segments: list[str] = []
    key = None
    for ln in lines:
        if ln.startswith("#EXT-X-KEY"):
            method = (_attr(ln, "METHOD") or "NONE").upper()
            if method == "NONE":
                key = None
                continue
            if "AES-128" not in method:
                raise DownloadError(f"暂不支持的加密方式: {method}")
            uri = _attr(ln, "URI")
            if not uri:
                raise DownloadError("EXT-X-KEY 缺少 URI")
            iv = _attr(ln, "IV")
            key = (method, urllib.parse.urljoin(base_url, uri), iv)
        elif not ln.startswith("#"):
            segments.append(urllib.parse.urljoin(base_url, ln))
    if not segments:
        raise DownloadError("m3u8 中没有切片")
    return {"kind": "media", "segments": segments, "key": key}


def _attr(line: str, name: str) -> str | None:
    import re
    m = re.search(rf'{name}=("[^"]*"|[^,]*)', line)
    if not m:
        return None
    return m.group(1).strip('"')


def _hls_python_download(url: str, out_path: Path, max_workers: int = 4) -> Path:
    """纯标准库 HLS 下载：滑动窗口并发拉切片，按序写盘，内存占用有界。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = _http_get(url).decode("utf-8", errors="replace")
    info = parse_m3u8(text, url)
    if info["kind"] == "master":
        return _hls_python_download(info["variants"][0], out_path, max_workers)

    key_info = info["key"]
    cipher_key = b""
    if key_info is not None:
        try:
            from Crypto.Cipher import AES
        except ImportError as e:
            raise DownloadError(
                "该回放流是 AES-128 加密 HLS。二选一："
                "pip install pycryptodome；或安装 ffmpeg 后重试（自动走 ffmpeg）") from e
        cipher_key = _http_get(key_info[1])[:16]

    segments: list[str] = info["segments"]
    _log.info("HLS 下载: %d 个切片，%d 线程", len(segments), max_workers)

    def fetch(seg_url: str) -> bytes:
        data = _http_get(seg_url)
        if key_info is not None:
            from Crypto.Cipher import AES as _AES
            iv = _iv_bytes(key_info[2], 0)
            return _AES.new(cipher_key, _AES.MODE_CBC, iv).decrypt(data)
        return data

    start = time.time()
    with out_path.open("wb") as f, ThreadPoolExecutor(max_workers=max_workers) as pool:
        window = min(max_workers * 2, len(segments))
        futures = [pool.submit(fetch, s) for s in segments[:window]]
        idx = 0
        while idx < len(segments):
            data = futures[0].result()  # 按序写盘
            futures.pop(0)
            f.write(data)
            idx += 1
            nxt = window + idx - 1
            if nxt < len(segments):
                futures.append(pool.submit(fetch, segments[nxt]))
            if idx % 50 == 0:
                _log.info("HLS 进度 %d/%d（%.0f 秒）", idx, len(segments),
                          time.time() - start)
    _log.info("HLS 下载完成: %s（%.1f MB）", out_path.name,
              out_path.stat().st_size / 1e6)
    return out_path


def _iv_bytes(iv: str | None, sequence: int) -> bytes:
    if iv:
        hexs = iv.removeprefix("0x")
        return bytes.fromhex(hexs.zfill(32))[:16]
    return sequence.to_bytes(16, "big")


def download_stream(url: str, out_path: Path) -> Path:
    """入口：m3u8/直链统一下载。有 ffmpeg 用 ffmpeg，否则纯标准库。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and out_path.stat().st_size > 0:
        _log.info("已存在缓存，跳过下载: %s", out_path)
        return out_path
    path_lower = urllib.parse.urlparse(url).path.lower()
    if shutil.which("ffmpeg"):
        return _ffmpeg_download(url, out_path)
    if path_lower.endswith(".m3u8"):
        return _hls_python_download(url, out_path)
    if path_lower.endswith((".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm")):
        return download_direct(url, out_path)
    raise DownloadError(f"无法识别的媒体 URL: {url}")
