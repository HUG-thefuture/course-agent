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

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# 函数式判题前置防护：用户代码提前 exit()/sys.exit()/os._exit() 会跳过追加的
# 断言并以退出码 0 误判"通过"，这里统一替换为显式报错
_PY_GUARD = """\
import builtins as _b, sys as _s, os as _o
def _no_exit(name):
    def _f(*a, **k):
        raise RuntimeError(f"在线判题中禁止调用 {name}()，请让程序自然结束")
    return _f
_b.exit = _no_exit("exit"); _b.quit = _no_exit("quit")
_s.exit = _no_exit("sys.exit"); _o._exit = _no_exit("os._exit")
"""

_JS_GUARD = ("process.exit = () => { throw new Error("
             "'在线判题中禁止调用 process.exit()'); };\n")


def _runner_env() -> dict:
    return {"PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8"}


def run_process(cmd: list[str], input_text: str = "", timeout: int = 10,
                env: dict | None = None) -> tuple[int | None, str, str, bool]:
    """运行子进程；超时杀死整棵进程树（防孙进程持有管道把超时无限拖长）。

    返回 (returncode, stdout, stderr, timed_out)。
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=env, **kwargs)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError):
            out, err = "", ""
        return proc.returncode, out or "", err or "", True


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(script: str, suffix: str, timeout: int = 10,
         stdin_data: str = "") -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / f"main{suffix}"
        p.write_text(script, encoding="utf-8")
        rc, out, err, timed_out = run_process(
            [sys.executable if suffix == ".py" else "node", str(p)],
            input_text=stdin_data, timeout=timeout, env=_runner_env())
        if timed_out:
            return False, f"超时（>{timeout}s），检查死循环或效率问题"
        return rc == 0, (out or err or "")[:500]


def judge_function(code: str, tests: list[str], language: str = "python",
                   timeout: int = 10) -> dict:
    """函数式判题：逐条断言独立运行。"""
    suffix = ".py" if language == "python" else ".js"
    pre = _PY_GUARD if language == "python" else (
        "function assert(c, m) { if (!c) "
        "throw new Error('AssertionError'); }\n" + _JS_GUARD)
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


def _norm_output(s: str) -> str:
    """按行剥尾部空白并去首尾空行（忽略行尾空白与换行风格差异）。"""
    lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def judge_stdio(code: str, samples: list[dict], language: str = "python",
                timeout: int = 10) -> dict:
    """标准 IO 判题：逐个样例比对输出（忽略行尾空白与换行风格）。"""
    suffix = ".py" if language == "python" else ".js"
    runner = sys.executable if language == "python" else "node"
    results = []
    for i, s in enumerate(samples):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"main{suffix}"
            p.write_text(code, encoding="utf-8")
            rc, out, err, timed_out = run_process(
                [runner, str(p)], input_text=s.get("input", ""),
                timeout=timeout, env=_runner_env())
            want = _norm_output(s.get("output") or "")
            got = _norm_output(out or "")
            ok = (not timed_out) and rc == 0 and got == want
            results.append({"idx": i, "input": s.get("input", ""),
                            "expected": want, "got": got[:400],
                            "passed": ok})
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
