# CourseAgent —— 直播课程知识整理助手

把直播课的**字幕 / 文稿 / 录音 / 网页**自动整理成：**知识点笔记、关键节点速查表、配套习题（含答案解析）**，全部产物经过"计划 → 审查 → 测试"质检闭环，帮助你课后快速掌握一门课。

- **本地大模型优先**（Ollama / LM Studio 等 OpenAI 兼容端点），可选外部 API 兜底（DeepSeek / 通义 / OpenAI 等）
- **自带质检闭环**：模型生成的内容必须通过结构校验 + 忠实度抽查，不通过自动回到"思考"阶段重做
- **核心零依赖**：只用 Python 标准库（3.10+），装上就能跑
- **图形界面 / 命令行双入口**：双击即用，也可脚本化批处理

---

## 零、下载与安装（两条路，任选其一）

> 本项目**不附带任何模型密钥**，装好后需自己配一个 OpenAI 兼容的模型服务（见「六、配置」）。

### 方式 A：下载现成版本（推荐给不想装 Python 的人）

**直接下载**：[CourseAgent v1.1](https://github.com/HUG-thefuture/course-agent/releases/latest)（Windows 10/11 64 位，无需装 Python）

两种版本按需选：

| 版本 | 文件 | 适用 |
|---|---|---|
| **单文件版** | `CourseAgent-v1.1.0-win64.exe`（15 MB） | 想走**安装向导、自选装到哪** |
| **目录版** | `CourseAgent-v1.1.0-portable.zip`（14 MB） | **解压即用**，不装任何东西 |

**单文件版**（支持自选安装位置）：

```bat
CourseAgent-v1.1.0-win64.exe --install      :: 弹安装向导，自选目录
CourseAgent-v1.1.0-win64.exe --uninstall    :: 卸载（只删快捷方式，数据保留）
```

向导会问装到哪个目录（默认避开空间紧张的 C 盘）、是否建桌面/开始菜单快捷方式。
静默安装：`CourseAgent.exe --install "D:\工具" --silent`

**目录版**：解压到任意目录 → 双击 `CourseAgent.exe`。

> **重要**：单文件版请用安装向导装到正常目录（如 `C:\CourseAgent`、`D:\工具`）。
> 不要直接丢在受 ACL 限制的文件夹里跑——PyInstaller 单文件版启动时要在 exe 同目录
> 解压临时文件，权限受限时会报 `Could not create temporary directory!` 直接退出
> （改 TEMP 环境变量、加 `--runtime-tmpdir` 都无效，实测过）。目录版没有这个限制。

**两个版本通用**：首次运行会在程序旁生成 `config.example.json`；配模型二选一：
- **本地模型**：装 [Ollama](https://ollama.com/download)，跑 `ollama pull qwen3:8b`，不用改配置
- **在线 API**：复制 `config.example.json` 为 `config.json`，填 `api_key` 与 `base_url`

想先验证环境可跑 `CourseAgent.exe demo`（离线演示，不需要模型）。

目录版解压后的结构：

```
CourseAgent/
├── CourseAgent.exe      ← 双击这个
├── config.example.json  ← 复制成 config.json 后填密钥
├── 先看我.txt           ← 精简版上手说明
├── data/                ← 运行时自动创建（缓存、记忆库）
└── output/              ← 整理出的课程笔记都在这
```

升级时用新版替换旧的即可，`data/`、`output/`、`config.json` 不受影响。

### 方式 B：源码运行（推荐给开发者 / 想改代码的人）

```bash
git clone https://github.com/HUG-thefuture/course-agent.git
cd course-agent
python main.py demo          # 离线演示，验证环境（不需要模型）
python main.py config        # 检查模型连通性
python -m course_agent.web   # 启动图形界面
```

环境要求：Python 3.10+。核心功能零第三方依赖；音视频转写需 `pip install faster-whisper`，PDF 需 `pip install pypdf`。

### 自己打包 exe

```bash
pip install pyinstaller

# 单文件版（一个 exe，15 MB，适合分发）
python tools/build_exe.py --onefile --to-desktop

# 目录版（启动更快，不受文件夹 ACL 限制）
python tools/build_exe.py --zip              # 产物在 dist/CourseAgent/

# 两个版本依次构建时，记得先给目录版改名，否则会被单文件版覆盖
python tools/build_exe.py --zip && mv dist/CourseAgent dist/CourseAgent-portable
python tools/build_exe.py --onefile --to-desktop
```

> 打包时会排除 `faster-whisper`/`onnxruntime` 等重型可选依赖（体积 365MB → 30MB）。
> 若需要本地音视频转写，加 `--with-whisper`。
>
> 注意别把 `tkinter` 放进 `--exclude-module`：安装向导要用它，
> 一边排除一边 hidden-import 会让包损坏。

---

## 一、快速开始

```bash
# 0. 环境要求：Python 3.10+；可选本地模型（如 Ollama）

# 1. 先跑离线演示（内置 Mock 模型，不需要任何模型和网络）
python main.py demo

# 2. 配置真实模型（本机已有 Ollama 的话，默认配置即可直连）
#    ollama pull qwen3:8b   # 推荐模型，默认配置即指向它
python main.py config        # 查看配置与本地/外部模型连通性

# 3. 整理一份课程材料
python main.py run --input examples/sample_lecture.srt --title "Spring Boot 事务与并发"
python main.py run --input 课程文稿.txt
python main.py run --input https://课程回放页 --password 页面密码
```

运行后在 `output/<会话ID>/` 下得到：

| 文件 | 内容 |
| --- | --- |
| `review.md` | **复习笔记（主文档）**：每个知识点含完整概念讲解、代码示例、易错点，文末附相关性拓展（课程外关联知识，已标注）——不回看视频也能复习掌握 |
| `notes.md` | 原始笔记（含时间戳定位、学习目标、关键节点表） |
| `key_nodes.md` | 关键节点速查表（重点演示 / 易错点 / 面试高频 / 作业布置…） |
| `exercises.md` | 配套习题：单选/多选/判断/填空/简答 + **编程实战题**（任务描述、起始代码骨架、思路提示在题目区；参考实现与验收要点清单在文末），题目在前、答案在后 |
| `sections.json` / `plan.json` | 结构化数据（可自建 Anki / 复习系统对接） |
| `review_report.md` | 质检报告：每轮审查/测试的问题与最终得分 |

## 二、支持的输入材料

| 类型 | 说明 |
| --- | --- |
| `.srt` / `.vtt` | 直播课字幕（推荐，能生成时间戳，可定位回放） |
| `.txt` / `.md` | 课程文稿、笔记 |
| `.json` | `[{"start": 秒, "text": "..."}]` 格式的转写结果 |
| 音视频 `.mp3/.mp4/…` | 本地转写（faster-whisper，已验证可用） |
| PDF 讲义 | 需 `pip install pypdf` |
| 网页 URL | 直接抓取正文；`--password` 会以查询参数/表单惯例传入 |
| 视频/回放流 URL | m3u8 / mp4 直链，自动下载（优先 ffmpeg）→ 转写 → 整理 |

## 三、腾讯会议课程怎么用（已实战验证）

腾讯会议直播中的实时字幕没有对学员开放的程序接口，**课后**素材走以下三条路。
用一条真实的回放链接（`meeting.tencent.com/crm/xxxx` + 提取码）完整验证过路径 A。

**路径 A · 逐字稿（强烈推荐，无需下载视频，自带逐句时间戳）**

腾讯会议回放页自带 AI 纪要（逐字稿），我们只需要把它的数据拿到手：

1. 浏览器打开回放分享链接，输入提取码，确认能看到「纪要 / 逐字稿」
2. F12 → Network → 筛选框输入 `minutes/detail`，右键该请求 → Copy → Copy link address
3. 把地址里的 `limit=20` 改成 `limit=1000`，浏览器直接访问，全选另存为 `minutes.json`
4. 转换并整理（转换器在 `tools/` 里，已随仓库提供）：

```bash
python tools/convert_tencent_minutes.py minutes.json transcript.json
python main.py run --input transcript.json --title "88、作业讲评"
```

**路径 B · 视频（有屏幕演示、板书的课程）**

Network 筛选 `mp4`：`recording-1_screen.mp4` 是屏幕共享、`recording-1_speaker.mp4`
是摄像头。复制带 token 的地址（有时效，尽快用）：

```bash
python main.py run --input "https://ylz.cos.meeting.tencent.com/cos/.../xxx_screen.mp4?token=..." --title "课程名"
# 或只下载+转写：python main.py fetch --input "<地址>"
```

下载优先调用本机 ffmpeg（已检测到 8.1.2）；无 ffmpeg 时自动退回纯标准库 HLS 下载。

**路径 C · 官方导出**：分享者勾选「允许下载」时直接在回放页下载 mp4 再 `run`；
老师能导出「会议转写」文稿的话 `run --input 文稿.txt` 更轻量。

> 无法自动化获取时，`fetch`/`run` 会打印以上分步引导。下载与整理仅限你合法
> 拥有访问权限的课程回放，请遵守分享者设置的权限。

## 四、批量处理与视频课程

**批量识别课程**（`batch` 命令）——目录、清单、链接混着来都行：

```bash
# 整个目录（音视频/字幕/文稿自动识别）
python main.py batch --input D:/课程录屏

# 清单文件（courses.list：每行 链接|提取码|标题，或 路径|标题）
# 或 courses.json：[{"source": "...", "password": "...", "title": "..."}]
python main.py batch --input courses.list

# 多个来源混合
python main.py batch --input D:/录屏 courses.json "https://meeting.tencent.com/crm/xxx"

# 断点续跑：已完成的自动跳过，中断后重跑同一命令即可接着干（--force 全部重跑）
```

逐个课程独立处理：**单个失败不中断整批**，产物分目录存放，`batch_report.json`
汇总每课状态与得分。同一视频只转写一次（缓存），二跑秒级完成。
全批共享记忆库，前面的课程范例会让后面的整理更稳。

**视频课程通道**（mp4/录屏直接喂）：

- 语音 → faster-whisper 本地转写（已验证）；
- 画面 → ffmpeg 场景检测提取关键帧，存档在产物 `frames/` 目录，做题时对照回看；
- **可选视觉模型**：`config.json` 里启用 `vision`（如本地 `ollama pull qwen2.5vl:7b`
  或云端 qwen-vl），帧会被模型"看"一遍，屏幕上演示的代码/命令生成文字记录，
  注入对应章节——**编程题因此能覆盖老师只演示没讲透的代码**。未启用时自动降级。

**不完整课程**：材料过短（低于 `pipeline.partial_chars`，默认 3000 字）自动按
"片段/节选"处理——提示计划阶段按现有内容组织、不假设缺失部分，短录屏、半节课
素材都能正常出产物（已用 96 秒短视频端到端验证）。

## 五、核心工作流（与你要求的流程一一对应）

```
素材加载 ──> ① 思考 ──> ② 出具计划(章节划分JSON)
                          │
                          ▼
                   ③ 审查计划（代码硬校验 + 模型语义审查）
                          │ 不通过 ──> 带审查意见回到 ①
                          ▼ 通过
                   ④ 按计划执行（逐节生成知识点/关键节点/习题）
                          │
                          ▼
                   ⑤ 测试（结构校验 + 忠实度抽查）
                          │ 不通过 ──> 带问题清单回到 ①
                          ▼ 通过
                   输出全部产物 + 沉淀高质量范例到记忆库
```

- 所有循环都有轮数上限（默认 3 轮），**保证必然终止**，不会死循环
- 审查与测试都采用"代码确定性校验优先，模型判断为辅"的双保险设计
- `config.json` 里 `max_attempts` 可调轮数，`review_source: "api"` 可让外部模型独立审查本地模型的产出，更客观

## 六、配置

复制 `config.example.json` 为 `config.json`（该文件含密钥已被 `.gitignore` 排除）：

```jsonc
{
  "prefer": "auto",            // auto: 本地优先失败回落 API；local / api 指定只用其一
  "review_source": "same",     // "api" = 用外部模型独立做审查/质检
  "local": {
    "base_url": "http://localhost:11434/v1",   // Ollama 默认；LM Studio 用 :1234/v1
    "model": "qwen3:8b"
  },
  "api": {
    "enabled": false,          // true 后本地模型失败自动回落到外部 API
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": ""              // 推荐留空，用环境变量 COURSE_AGENT_API_KEY 传入
  },
  "vision": {                  // 可选：读视频屏幕帧（编程题证据增强）
    "enabled": false,
    "base_url": "http://localhost:11434/v1",
    "model": "qwen2.5vl:7b"    // ollama pull qwen2.5vl:7b 后即可启用
  },
  "pipeline": {
    "partial_chars": 3000      // 低于此字数按"不完整课程"处理
  }
}
```

API 密钥**只**从 `config.json`（不入库）或环境变量 `COURSE_AGENT_API_KEY` 读取，永远不会出现在代码与提交记录中。

## 七、“训练”：助手如何越用越好

本项目不做烧卡的权重微调，而是两条实证有效的路径（`improve.py`）：

1. **范例沉淀 + 相似度召回**：质检得分 ≥70 的章节产物自动进入记忆库（`data/memory.db`），
   之后处理同主题内容时自动召回为少样本示例注入提示词，格式与内容稳定性显著提升；
2. **评估循环**：`improve` 命令在样例材料上多轮"生成→质检→沉淀"，得分曲线写入 `data/metrics.jsonl`：

```bash
python main.py improve --input examples/sample_lecture.txt --rounds 3
# 用你的真实打分反向校准：
python main.py rate --session <会话ID> --section <章节名> --score 4 --note "习题偏简单"
# 想真正微调模型？把范例库一键导出为 Alpaca 格式，接入 LLaMA-Factory / PEFT 做 LoRA：
python main.py export-sft --out data/sft.jsonl
```

### 存量课程升级复习笔记

早期批次的笔记结构较旧时，可按原章节边界重生成复习笔记（不重新出题）：

```bash
python tools/regen_review.py --output "output/特训营全部课程" --transcripts data/harvest_transcripts
```

## 八、测试

```bash
python -m unittest discover -s tests -v
```

106 个用例覆盖：JSON 容错提取、字幕/HTML 解析、分块完整性、HTTP 重试与回落、
计划/审查/测试各阶段校验、编排循环的失败恢复与终止保证、记忆库容量治理、CLI 端到端。
全部离线运行（Mock 模型），无需网络与本地模型。

## 九、工程保障（无逻辑错误 / 无内存泄漏）

- **循环必然终止**：外层 `max_attempts` 上限 + 内部重试上限，失败路径全部有出口
- **资源零堆积**：HTTP 用 `urllib` 每请求独立连接（用完即关）；文件全部 `with` 托管；
  sqlite 连接显式关闭；转写模型用完即释放
- **容量有上限**：范例库按分数滚动保留 200 条；反馈/会话表按主键去重；日志无全局累积
- **输入容错**：GBK/UTF-8 自动识别、残缺 HTML 容忍解析、模型输出围栏/思考段/杂文均能提取
- **异常有边界**：CLI 层统一捕获业务异常返回非零退出码，方便脚本化调用

## 十、目录结构

```
course-agent/
├── main.py                  # 入口
├── config.example.json      # 配置样例（复制为 config.json 使用）
├── course_agent/
│   ├── cli.py               # 命令行（run/demo/improve/rate/export-sft/config）
│   ├── config.py            # 配置加载与连通性检查
│   ├── prompts.py           # 提示词加载（course_agent/prompts/*.md 可直接改）
│   ├── pipeline/            # 编排器 + 计划/审查/执行/测试 五阶段
│   ├── ingest/              # 字幕/文本/网页/PDF/音视频加载与分块
│   ├── llm/                 # OpenAI 兼容客户端 + 路由回落 + Mock
│   ├── memory.py            # sqlite 记忆库（会话/范例/反馈）
│   ├── improve.py           # 自改进循环与 SFT 导出
│   └── outputs.py           # Markdown/JSON 产物渲染
├── prompts(内置)/           # 六个阶段提示词，改这里即可定制行为
├── examples/                # 示例课程（文稿 + 字幕）
└── tests/                   # 83 个离线单元测试
```

## 十一、已知边界

- 实时直播中的**同步**字幕抓取因平台而异，本工具面向**课后材料**（录屏、字幕、文稿）整理
- 音视频转写首次使用需下载 whisper 模型权重（约 500MB，CPU 可跑）
- 忠实度抽查是抽样而非全量，极端情况下个别编造内容可能漏检——可用 `rate` 命令反馈，
  低分章节不会进入范例库
