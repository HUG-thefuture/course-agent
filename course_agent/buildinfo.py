"""构建标识：用于判断「正在运行的 GUI 进程」是否比磁盘上的代码旧。

长期运行的本地服务（pythonw 后台进程）会把模块加载进内存；源码更新后，
旧进程仍在服务，用户看到的行为就还是修之前的。启动器用本模块的指纹比对
「运行中实例」与「磁盘代码」，不一致就重启。

指纹只取源文件 (路径, mtime_ns, size)，不做内容哈希——足够区分版本，且极快。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_PKG = Path(__file__).resolve().parent
_ROOT = _PKG.parent


def build_id(root: Path | None = None) -> str:
    """course_agent 包内全部 .py 的版本指纹（12 位十六进制）。"""
    base = Path(root) if root is not None else _PKG
    rows: list[str] = []
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append(f"{p.relative_to(base).as_posix()}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()[:12]


def project_root() -> Path:
    return _ROOT
