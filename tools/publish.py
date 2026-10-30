"""一键发布：安全自检 → 提交 → 推送。

安全自检是硬门槛：只要发现疑似密钥，脚本直接中止并列出命中文件。
检查项：
  1) 待提交/已跟踪文件里是否有 resembles-API-key 的字符串；
  2) .gitignore 是否覆盖 config.json / *.bak / data / output；
  3) 历史提交（可用 --skip-history 跳过，较慢）。

用法：
  python tools/publish.py --check          # 只做安全自检
  python tools/publish.py -m "提交说明"     # 自检通过后提交并推送
  python tools/publish.py -m "..." --dry-run  # 只展示将提交什么
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 形如真实密钥的模式（保守起见宁可误报）
SECRET_PATTERNS = [
    r"nvapi-[A-Za-z0-9_\-]{20,}",
    r"sk-[A-Za-z0-9]{24,}",
    r"rc-[a-f0-9]{30,}",
    r"ghu_[A-Za-z0-9]{30,}",
    r"ghp_[A-Za-z0-9]{30,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",
]
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in SECRET_PATTERNS))

# 必须被 .gitignore 覆盖的路径
MUST_IGNORE = ["config.json", "config.json.bak", "data/", "output/", "*.log"]

# 允许出现"密钥占位符"的文件（示例配置本来就要写说明）
ALLOWLIST = {".gitignore", "README.md", "config.example.json"}


def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def staged_and_tracked_files() -> list[Path]:
    """待提交（暂存 + 未跟踪未忽略）与已跟踪的文件。"""
    out: set[str] = set()
    r = git("ls-files", "-co", "--exclude-standard")
    for line in (r.stdout or "").splitlines():
        if line.strip():
            out.add(line.strip())
    r2 = git("ls-files")
    for line in (r2.stdout or "").splitlines():
        if line.strip():
            out.add(line.strip())
    return [ROOT / f for f in sorted(out)]


def scan_files(files: list[Path]) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for p in files:
        if not p.is_file():
            continue
        if p.name in ALLOWLIST or p.suffix in (".png", ".jpg", ".ico", ".zip"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = _SECRET_RE.search(line)
            if m:
                masked = m.group(0)[:8] + "***"
                hits.append((str(p.relative_to(ROOT)), i, masked))
    return hits


def check_ignore_rules() -> list[str]:
    problems: list[str] = []
    for pat in MUST_IGNORE:
        r = git("check-ignore", pat)
        if r.returncode != 0:
            problems.append(pat)
    return problems


def scan_history() -> list[str]:
    r = git("log", "--all", "-p", "--", ".")
    hits: list[str] = []
    for line in (r.stdout or "").splitlines():
        if line.startswith("+") and _SECRET_RE.search(line):
            hits.append(line[:120])
            if len(hits) >= 5:
                break
    return hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="安全自检 + 提交 + 推送")
    ap.add_argument("-m", "--message", default="", help="提交说明")
    ap.add_argument("--check", action="store_true", help="只做自检")
    ap.add_argument("--dry-run", action="store_true", help="只展示，不提交")
    ap.add_argument("--skip-history", action="store_true", help="跳过历史扫描（快）")
    ap.add_argument("--no-push", action="store_true", help="提交但不推送")
    args = ap.parse_args(argv[1:])

    print("[1/3] 扫描待提交与已跟踪文件…")
    files = staged_and_tracked_files()
    print(f"      共 {len(files)} 个文件")
    hits = scan_files(files)
    if hits:
        print("\n✗ 发现疑似密钥，已中止：")
        for f, ln, masked in hits:
            print(f"    {f}:{ln}  {masked}")
        print("\n处理方式：把该文件加进 .gitignore，或把密钥换成占位符后重试。")
        return 1
    print("      ✓ 未发现密钥")

    print("[2/3] 检查 .gitignore 覆盖…")
    missing = check_ignore_rules()
    if missing:
        print(f"\n✗ 以下路径未被忽略（含密钥/产物的风险）：{missing}")
        return 1
    print("      ✓ 覆盖完整")

    if not args.skip_history:
        print("[3/3] 扫描历史提交…")
        hist = scan_history()
        if hist:
            print("\n✗ 历史提交中发现疑似密钥：")
            for h in hist:
                print(f"    {h}")
            print("\n需要用 git filter-repo 清理历史，或改用新的仓库。")
            return 1
        print("      ✓ 历史干净")
    else:
        print("[3/3] 已跳过历史扫描")

    if args.check:
        print("\n✓ 安全自检通过")
        return 0

    if not args.message:
        print("\n请用 -m 提供提交说明")
        return 1

    print(f"\n暂存变更…")
    git("add", "-A")
    r = git("diff", "--cached", "--stat")
    print(r.stdout or "（无变更）")
    if args.dry_run:
        print("\n--dry-run：未提交")
        return 0

    r = git("commit", "-m", args.message)
    if r.returncode != 0:
        print("提交失败：", r.stdout, r.stderr)
        return 1
    print((r.stdout or "").strip())

    if args.no_push:
        print("\n已提交（未推送）")
        return 0

    print("\n推送到 origin…")
    r = git("push", "origin", "HEAD")
    if r.returncode != 0:
        print("推送失败：", r.stdout, r.stderr)
        return 1
    print((r.stdout or r.stderr or "").strip())
    print("\n✓ 发布完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
