"""云端工作进程值守：等待 config.json 里出现外部模型 Key 后自动开工。

流程：
  1. 轮询 config.json 的 api.api_key（每 2 分钟，最长 24 小时）；
  2. Key 出现后先冒烟测试一条真实补全，验证连通与模型名；
  3. 计算"剩余未完成"的转写稿，划出约 40% 给云端（本地链路处理其余）；
  4. 以 --source api 启动批量（api 优先，额度耗尽自动回落本地）。

用法：python tools/cloud_worker_watch.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import load_config  # noqa: E402


def key_ready() -> str:
    cfg = load_config()
    return (cfg.api_key_effective or "").strip()


def smoke_test(cfg) -> tuple[bool, str]:
    import urllib.request
    body = json.dumps({
        "model": cfg.api.model,
        "messages": [{"role": "user", "content": "回复两个字：收到"}],
        "max_tokens": 16, "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        cfg.api.base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.api_key_effective}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return True, truncate_text(text, 40)
    except Exception as e:
        return False, str(e)[:200]


def truncate_text(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def remaining_courses(cfg) -> list[Path]:
    """无 session.json 产物的转写稿（本地链路尚未完成的部分）。"""
    out = Path(cfg.output_dir)
    done_stems: set[str] = set()
    if out.is_dir():
        for d in out.rglob("session.json"):
            stem = d.parent.name.split("-", 1)[-1]  # 去掉批量序号前缀
            done_stems.add(stem)
    tdir = Path(cfg.data_dir) / "harvest_transcripts"
    remaining = []
    for f in sorted(tdir.glob("*.json")):
        if f.stem == "harvest_report":
            continue
        if f.stem not in done_stems:
            remaining.append(f)
    return remaining


def main() -> int:
    print("云端工作进程值守启动：等待 config.json 中出现外部模型 Key…", flush=True)
    key = ""
    for _ in range(720):  # 最长 24 小时
        key = key_ready()
        if key:
            break
        time.sleep(120)
    if not key:
        print("等待超时，退出")
        return 1

    cfg = load_config()
    print(f"检测到 Key（{key[:6]}***），冒烟测试 {cfg.api.model} …", flush=True)
    ok, detail = False, ""
    for attempt in range(1, 21):  # 服务商有并发限制，失败自动重试
        ok, detail = smoke_test(cfg)
        if ok:
            break
        print(f"冒烟第 {attempt} 次失败（{detail}），60 秒后重试", flush=True)
        time.sleep(60)
    if not ok:
        print("冒烟连续失败，退出", flush=True)
        return 1
    print(f"冒烟测试通过：{detail}", flush=True)

    remaining = remaining_courses(cfg)
    if not remaining:
        print("没有剩余课程，无需云端协助")
        return 0
    take = max(1, round(len(remaining) * 0.4))
    cloud_part = remaining[-take:]          # 云端从队尾往前吃
    cloud_list = Path(cfg.data_dir) / "cloud_partition.list"
    cloud_list.write_text("\n".join(str(f) for f in cloud_part), encoding="utf-8")
    print(f"剩余 {len(remaining)} 门：云端领 {take} 门，本地链路处理其余", flush=True)

    cmd = ["main.py", "batch", "--input", str(cloud_list),
           "--outdir", "output/特训营全部课程", "--source", "api"]
    print(f">>> 启动云端批量：{' '.join(cmd)}", flush=True)
    return subprocess.run([sys.executable, *cmd], cwd=str(ROOT)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
