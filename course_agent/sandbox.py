"""轻量判题沙箱（方案 A，Windows 原生隔离，零依赖）。

与 webui 的 run_code_tests 区别：支持 stdin 样例判题（标准 IO 模式）、
限制单次运行时长、隔离临时目录、默认禁用网络环境变量注入。
安全边界（如实说明）：非容器级隔离，仅供个人本机使用；
如需更强隔离，后续升级 Docker 方案（阶段四可选）。

判题模式：
  function  拼接用户代码 + assert 断言（函数式）
  stdio     样例输入输出逐个比对（标准 IO 模式）
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


def _run(script: str, suffix: str, timeout: int = 10,
         stdin_data: str = "") -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / f"main{suffix}"
        p.write_text(script, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable if suffix == ".py" else "node", str(p)],
                input=stdin_data, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
                env={"PATH": p.parent.as_posix() + ";" +
                     __import__("os").environ.get("PATH", ""),
                     "PYTHONIOENCODING": "utf-8"})
            return proc.returncode == 0, (proc.stdout or proc.stderr or "")[:500]
        except subprocess.TimeoutExpired:
            return False, f"超时（>{timeout}s），检查死循环或效率问题"
        except FileNotFoundError:
            return False, "运行器不存在（node 未安装？）"


def judge_function(code: str, tests: list[str], language: str = "python",
                   timeout: int = 10) -> dict:
    """函数式判题：逐条断言独立运行。"""
    suffix = ".py" if language == "python" else ".js"
    pre = ""
    if language == "javascript":
        pre = ("function assert(c, m) { if (!c) "
               "throw new Error('AssertionError'); }\n")
    results = []
    for i, t in enumerate(tests):
        script = pre + code.rstrip() + "\n\n" + t + "\n"
        ok, out = _run(script, suffix, timeout)
        results.append({"idx": i, "test": t, "passed": ok,
                        "output": out[:400]})
    passed = sum(1 for r in results if r["passed"])
    return {"runnable": True, "results": results,
            "passed": passed, "total": len(results),
            "mode": "function"}


def judge_stdio(code: str, samples: list[dict], language: str = "python",
                timeout: int = 10) -> dict:
    """标准 IO 判题：逐个样例比对输出（忽略行尾空白）。"""
    suffix = ".py" if language == "python" else ".js"
    results = []
    for i, s in enumerate(samples):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"main{suffix}"
            p.write_text(code, encoding="utf-8")
            runner = sys.executable if language == "python" else "node"
            try:
                proc = subprocess.run([runner, str(p)], input=s.get("input", ""),
                                      capture_output=True, text=True,
                                      timeout=timeout, encoding="utf-8",
                                      errors="replace")
                got = (proc.stdout or "").strip()
                want = (s.get("output") or "").strip()
                ok = proc.returncode == 0 and got == want
                results.append({"idx": i, "input": s.get("input", ""),
                                "expected": want, "got": got[:400],
                                "passed": ok})
            except subprocess.TimeoutExpired:
                results.append({"idx": i, "input": s.get("input", ""),
                                "expected": s.get("output", ""), "got": "",
                                "passed": False})
    passed = sum(1 for r in results if r["passed"])
    return {"runnable": True, "results": results,
            "passed": passed, "total": len(results), "mode": "stdio"}


def judge(code: str, exercise: dict, language: str = "python",
          timeout: int = 10) -> dict:
    """统一判题入口：有 tests 走函数式，有 samples 走标准 IO，否则不可判。"""
    tests = exercise.get("tests") or []
    samples = exercise.get("samples") or []
    if tests:
        return judge_function(code, tests, language, timeout)
    if samples:
        return judge_stdio(code, samples, language, timeout)
    return {"runnable": False,
            "reason": "本题无可执行断言/样例（框架或页面类题目请交卷由 AI 评审）"}
