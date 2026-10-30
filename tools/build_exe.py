"""打包 CourseAgent 为免安装 exe（PyInstaller --onedir）。

设计取舍：
  - 用 --onedir 而不是 --onefile：onefile 每次启动都要把上百 MB 解压到临时目录，
    冷启动 5-10 秒且杀软误报率高；onedir 启动即时、目录结构清晰。
  - 只打包 course_agent 包 + main.py；data/ output/ config.json **绝不入包**
    （含用户真实密钥与课程产物），首次运行时自动创建。
  - 随包附带 config.example.json 与 README，方便他人自行配置模型。

用法：
  python tools/build_exe.py                # 构建到 dist/CourseAgent/
  python tools/build_exe.py --zip          # 构建后打包成 zip 到桌面
  python tools/build_exe.py --clean        # 先清理 build/ dist/ 再构建
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "CourseAgent"
DESKTOP = Path.home() / "Desktop"

# 入口：双击无参数 → 开图形界面；带子命令 → 走命令行。
# 两者都要有：GUI 面向普通用户，CLI 面向批处理与排错（config / demo / run / fetch）。
ENTRY_SRC = '''"""CourseAgent 统一入口（打包用）。

无参数启动图形界面；带子命令走命令行，例如：
    CourseAgent.exe                      # 打开 GUI
    CourseAgent.exe config               # 查看配置与连通性
    CourseAgent.exe demo                 # 离线演示
    CourseAgent.exe run --input x.srt    # 整理单个文件
"""
import sys
from pathlib import Path

# 单文件版打包进来的示例配置（解压在临时目录），首次运行时拷到 exe 旁边，
# 让用户知道该改哪个文件。
EXAMPLE_NAME = "config.example.json"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_example_config() -> None:
    """首次运行：程序目录没有 config.json 就放一份示例，并提示怎么改。"""
    target = app_dir() / EXAMPLE_NAME
    if target.exists():
        return
    candidates = [
        Path(getattr(sys, "_MEIPASS", "")) / EXAMPLE_NAME,   # onefile 解压目录
        Path(__file__).resolve().parent / EXAMPLE_NAME,      # onedir 同级
    ]
    for src in candidates:
        try:
            if src.is_file():
                target.write_bytes(src.read_bytes())
                print(f"[提示] 已生成配置模板：{target}")
                print(f"       需要改模型的话，复制成 config.json 再编辑。")
                return
        except OSError:
            continue


def main() -> int:
    args = sys.argv[1:]
    ensure_example_config()

    if not args:
        from course_agent.webui import main as gui_main
        return gui_main([])

    # 图形界面的启动参数（--port / --no-browser）也归 GUI；其余交给 CLI
    GUI_FLAGS = ("--port", "--no-browser")
    if any(a == flag or a.startswith(flag + "=") for flag in GUI_FLAGS for a in args):
        from course_agent.webui import main as gui_main
        return gui_main(args)

    from course_agent.cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
'''

# 随包文件：(源, 包内目标目录)
BUNDLE_FILES = [
    ("config.example.json", "."),
    ("README.md", "."),
    ("requirements.txt", "."),
]


# 可选的重型依赖：本机装了 fater-whisper/scipy/grpc 等时，PyInstaller 会把它们
# 连同一堆 dll（ctranslate2 56MB、onnxruntime 36MB、scipy 20MB、avcodec 18MB…）
# 一起拖进包，体积从 40MB 膨胀到 360MB。这些只服务于"本地音视频转写"这一个可选
# 功能，教学/分享版默认排除；需要转写的人用 --with-whisper 构建完整版。
OPTIONAL_EXCLUDES = [
    "faster_whisper", "ctranslate2", "onnxruntime", "tokenizers", "transformers",
    "torch", "torchaudio", "numpy", "scipy", "pandas", "matplotlib",
    "grpc", "google", "pypdf", "chromadb", "sklearn", "PIL", "cv2",
    "IPython", "jupyter", "notebook", "pytest", "setuptools", "pip",
    "tkinter", "test", "unittest",
]


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def prepare_entry(tmp: Path) -> Path:
    p = tmp / "courseagent_gui.py"
    p.write_text(ENTRY_SRC, encoding="utf-8")
    return p


def run_pyinstaller(entry: Path, clean: bool, with_whisper: bool,
                    onefile: bool = False) -> int:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if onefile else "--onedir",
        "--name", APP_NAME,
        "--console",                      # 保留控制台：日志可见，排错友好
        "--paths", str(ROOT),
        # 只收集本项目的包，不把整个环境塞进去
        "--hidden-import", "course_agent.webui",
    ]
    if not with_whisper:
        for mod in OPTIONAL_EXCLUDES:
            args += ["--exclude-module", mod]
    if clean:
        args.append("--clean")

    # 运行时数据文件必须显式打进去，PyInstaller 不会自动收集非 .py 资源：
    #   prompts/*.md  —— prompts.py 用 Path(__file__).parent/'prompts' 读取，
    #                    缺了任何一个阶段都跑不起来（"提示词文件缺失"）；
    #   examples/     —— demo 命令要读 sample_lecture.srt。
    # 目标路径对应打包后的真实布局：_internal/course_agent/prompts、_internal/examples。
    sep = ";" if sys.platform == "win32" else ":"
    prompts = ROOT / "course_agent" / "prompts"
    if prompts.is_dir():
        args += ["--add-data", f"{prompts}{sep}course_agent/prompts"]
    ex = ROOT / "examples"
    if ex.is_dir():
        args += ["--add-data", f"{ex}{sep}examples"]
    # 示例配置随包带上：onedir 会拷到根目录给用户改；onefile 首次运行时
    # 由入口脚本从解压目录拷到 exe 旁边。
    example_cfg = ROOT / "config.example.json"
    if example_cfg.is_file():
        args += ["--add-data", f"{example_cfg}{sep}."]
    args.append(str(entry))
    log("执行: " + " ".join(args[3:]))
    return subprocess.run(args, cwd=str(ROOT)).returncode


def post_process(dist_dir: Path) -> None:
    """打包后处理：创建运行时目录 + 把用户要看的文件摆到根目录。

    注意：PyInstaller 的 --add-data 会把文件放进 _internal/（程序内部目录），
    用户根本看不见。而 config.example.json / README 必须摆在 exe 同级，
    否则「复制 config.example.json 改名 config.json」这条指引无从下手。
    所以这里直接从源码目录拷到根目录，并把 _internal 里的副本删掉避免混淆。
    """
    (dist_dir / "data").mkdir(exist_ok=True)
    (dist_dir / "output").mkdir(exist_ok=True)

    # 1) 用户可见文件放根目录
    for name in ("config.example.json", "README.md", "requirements.txt"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, dist_dir / name)
            log(f"已放置 {name} 到根目录")
        # 清掉 _internal 里的同名副本（那是 --add-data 的副产物）
        dup = dist_dir / "_internal" / name
        if dup.is_file():
            dup.unlink()

    readme = dist_dir / "先看我.txt"
    readme.write_text(
        """CourseAgent —— 免安装版使用说明
================================

【怎么启动】
  双击 CourseAgent.exe，浏览器会自动打开操作界面
  （地址 http://127.0.0.1:8686）。

【第一次用：配置模型】
  本程序不自带大模型，需要你自己准备一个"OpenAI 兼容"的模型服务。
  二选一：

  A. 本地模型（免费、数据不出本机，推荐）
     1) 装 Ollama：https://ollama.com/download
     2) 命令行执行：ollama pull qwen3:8b
     3) 不用改任何配置，直接启动即可（默认连 http://localhost:11434/v1）

  B. 在线 API（更快、效果更好，按量付费）
     把同目录下的 config.example.json 复制一份改名为 config.json，
     用记事本打开，找到 "api" 这一段，填上你的服务和密钥：
         "enabled": true,
         "base_url": "https://api.deepseek.com/v1",
         "model": "deepseek-chat",
         "api_key": "你的密钥"
     目标服务只要是 OpenAI 兼容接口都行（DeepSeek、通义、Kimi、
     Moonshot、OpenAI 官方等）。

  改完在界面点"新任务"，或命令行执行：CourseAgent.exe config  查看连通性。

【怎么用】
  打开界面后：
    1. 点上方「新任务」
    2. 粘贴课程链接、或者选择本地的字幕/文稿文件
    3. 点「开始整理」，等待完成
    4. 左侧「课程」里点开，看笔记、关键节点、习题（附答案）

  腾讯会议回放：直接粘贴分享链接即可自动提取逐字稿。
  若链接带提取码，写成：  链接 提取码 课程标题
  或直接粘贴会议纪要块（录制: 标题 / 录制文件: 链接 / 密码: 码）

【产物在哪】
  同目录下的 output 文件夹，每门课一个子文件夹，里面是：
    复习笔记 / 原始笔记 / 关键节点 / 习题 / 质检报告

【命令行用法（可选）】
  CourseAgent.exe run --input 课程.srt --title "课程名"
  CourseAgent.exe fetch --input "回放链接" --password 提取码
  CourseAgent.exe config
  CourseAgent.exe demo        （离线演示，不需要模型）

【常见问题】
  · 提示"所有模型均调用失败" → 模型服务没启动或密钥错，跑一次 config 看详情
  · 提示"需要提取码" → 该回放设了密码，把提取码填进输入框
  · 想改用别的模型 → 编辑 config.json 的 api.local 两段
  · 升级 → 用新版整个文件夹替换旧的即可，data/ output/ config.json 不受影响

更多说明见 README.md
""",
        encoding="utf-8",
    )
    log(f"已写入说明：{readme}")


def make_zip(dist_root: Path) -> Path:
    stamp = time.strftime("%Y%m%d")
    out = DESKTOP / f"CourseAgent-免安装版-{stamp}.zip"
    log(f"打包 zip -> {out}")
    shutil.make_archive(str(out.with_suffix("")), "zip", str(dist_root.parent),
                        dist_root.name)
    return out


def build_onefile(entry: Path, clean: bool) -> Path | None:
    """单文件版：产出 dist/CourseAgent.exe，直接拷到桌面即可单独分发。

    注意单文件版的代价：每次启动要把约 25MB 资源解压到临时目录，
    冷启动约 3-5 秒（比 onedir 慢），且部分杀软对 onefile 更敏感。
    好处是"就一个 exe"，适合发给不折腾的人。
    """
    log("构建单文件版…")
    rc = run_pyinstaller(entry, clean, with_whisper=False, onefile=True)
    if rc != 0:
        log(f"单文件构建失败，退出码 {rc}")
        return None
    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    if not exe.is_file():
        log("未找到 dist/CourseAgent.exe")
        return None
    # 单文件版把说明文件一起带上没意义（用户只拿到一个 exe），
    # 所以改成启动时自检：config.json 不存在就在程序目录生成一份示例。
    log(f"单文件完成：{exe}（{exe.stat().st_size / 1e6:.1f} MB）")
    return exe


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="打包 CourseAgent 免安装版")
    ap.add_argument("--clean", action="store_true", help="先清理 build/ dist/")
    ap.add_argument("--zip", action="store_true", help="构建后打成 zip 放桌面")
    ap.add_argument("--onefile", action="store_true",
                    help="构建单文件 exe（一个文件直接分发，启动稍慢）")
    ap.add_argument("--to-desktop", action="store_true",
                    help="把产物直接拷到桌面")
    ap.add_argument("--with-whisper", action="store_true",
                    help="包含本地音视频转写依赖（体积约 350MB）")
    args = ap.parse_args(argv[1:])

    if args.clean:
        for d in ("build", "dist"):
            p = ROOT / d
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                log(f"已清理 {d}/")

    tmp = ROOT / "build" / "_entry"
    tmp.mkdir(parents=True, exist_ok=True)
    entry = prepare_entry(tmp)

    if args.onefile:
        exe = build_onefile(entry, args.clean)
        if exe is None:
            return 1
        if args.to_desktop:
            dest = DESKTOP / exe.name
            shutil.copy2(exe, dest)
            log(f"已拷到桌面：{dest}")
        return 0

    rc = run_pyinstaller(entry, args.clean, args.with_whisper)
    if rc != 0:
        log(f"PyInstaller 失败，退出码 {rc}")
        return rc

    dist_dir = ROOT / "dist" / APP_NAME
    if not dist_dir.is_dir():
        log("未找到构建产物 dist/CourseAgent/")
        return 1
    post_process(dist_dir)

    exe = dist_dir / f"{APP_NAME}.exe"
    if exe.is_file():
        size = exe.stat().st_size / 1e6
        total = sum(f.stat().st_size for f in dist_dir.rglob("*") if f.is_file()) / 1e6
        log(f"完成：{exe}（主程序 {size:.1f} MB，整目录 {total:.1f} MB）")
    else:
        log("警告：未找到 exe")
        return 1

    if args.zip:
        z = make_zip(dist_dir)
        log(f"已生成：{z}（{z.stat().st_size / 1e6:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
