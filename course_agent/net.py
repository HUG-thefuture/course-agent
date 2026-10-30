"""统一外网请求：直连优先，失败自动回落系统代理。

背景：Windows 下 `urllib.request` 默认读取 WinINET 系统代理。当系统里残留指向
未运行代理（如 Clash `127.0.0.1:7897`）的配置时，`urlopen` 会向该端口发起连接
而被拒绝（`[WinError 10061] 由于目标计算机积极拒绝，无法连接`），导致腾讯会议
回放提取、网页抓取、视频下载、LLM 调用全部失败。

本模块的策略是「双向自愈」：
  1. 先直连（绕过系统代理）——项目的外网目标（腾讯会议、LLM 端点）与本地服务
     （Ollama）通常都能直连；
  2. 直连失败（连接被拒/超时/DNS 失败等 URLError）时，自动回退到系统代理再试一次，
     以兼容"确实必须走代理"的网络环境。

用法：
    from course_agent.net import open_direct, install_direct_opener
    with open_direct(req, timeout=30) as resp: ...
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)

_DIRECT_OPENER: urllib.request.OpenerDirector | None = None
# 按主机记录「直连不通」：某台机器直连失败时只影响它自己，
# 不会因为本地 Ollama 没开就把腾讯会议等外网请求也改走（可能失效的）系统代理。
_DIRECT_BROKEN_HOSTS: set[str] = set()


def direct_opener() -> urllib.request.OpenerDirector:
    """返回一个忽略系统代理的 opener（进程内单例）。

    以空代理字典构造 ProxyHandler，open 时不会调用 getproxies()，
    从而彻底绕开残留的失效系统代理。
    """
    global _DIRECT_OPENER
    if _DIRECT_OPENER is None:
        _DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _DIRECT_OPENER


def install_direct_opener() -> None:
    """把直连 opener 安装为进程级默认（供未显式使用 open_direct 的第三方调用）。"""
    urllib.request.install_opener(direct_opener())


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("127.")


def open_direct(req: urllib.request.Request, timeout: float | None = None):
    """直连优先打开请求；直连失败时回退系统代理，再失败则抛出异常。

    - 本机地址（localhost/127.x）永不走代理：系统代理配置错误时会连本地 Ollama、
      本地 GUI 都连不上，这类请求必须直连；
    - 外网地址直连失败时才回退系统代理，且「直连不通」按主机记忆——本地服务连不上
      不会连累腾讯会议的请求。
    返回对象与 `urlopen` 一致（可作上下文管理器）。
    """
    host = ""
    try:
        host = (req.host or "").split(":")[0].lower()
    except Exception:
        pass

    if _is_loopback(host):
        return direct_opener().open(req, timeout=timeout)   # 本机绝不能走代理

    if host not in _DIRECT_BROKEN_HOSTS:
        try:
            return direct_opener().open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise  # 服务端已应答（4xx/5xx），与代理无关，直接上抛
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _DIRECT_BROKEN_HOSTS.add(host)
            _log.warning("直连 %s 失败，回退系统代理重试：%s", host or "?", e)

    # 回退：按「当前」系统代理设置现场构建 opener。
    # 注意不能直接用 urllib.request.urlopen：它的全局 opener 在进程内首次调用时
    # 就把当时的代理配置固化了；若进程启动时代理可用而随后关闭，就会一直连不上。
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler())
        return opener.open(req, timeout=timeout)
    except Exception:
        # 代理也不通时撤销该主机的标记，让后续请求重新尝试直连（网络可能只是一时抖动）
        _DIRECT_BROKEN_HOSTS.discard(host)
        raise
