# CourseAgent —— 直播课程知识整理助手

把直播课的**字幕 / 文稿 / 录音 / 网页**自动整理成：**知识点笔记、关键节点速查表、配套习题（含答案解析）**，全部产物经过"计划 → 审查 → 测试"质检闭环，帮助你课后快速掌握一门课。

- **本地大模型优先**（Ollama / LM Studio 等 OpenAI 兼容端点），可选外部 API 兜底（DeepSeek / 通义 / OpenAI 等）
- **自带质检闭环**：模型生成的内容必须通过结构校验 + 忠实度抽查，不通过自动回到"思考"阶段重做
- **核心零依赖**：只用 Python 标准库（3.10+），装上就能跑

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
| `notes.md` | 知识点笔记（带时间戳定位、学习目标、关键节点表） |
| `key_nodes.md` | 关键节点速查表（重点演示 / 易错点 / 面试高频 / 作业布置…） |
| `exercises.md` | 配套习题，题目在前、参考答案与解析在文末 |
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

## 三、腾讯会议课程怎么用

腾讯会议直播中的实时字幕没有对学员开放的程序接口，**课后**素材走以下三条路（按推荐顺序）：

**路径 A（最简单）：回放页直接下载**
若老师分享回放时勾选了「允许下载」，在回放页直接下载 mp4，然后：
```bash
python main.py run --input 录制.mp4 --title "课程名"
```

**路径 B（全自动）：回放分享链接 + 提取码**
```bash
python main.py fetch --input "https://meeting.tencent.com/v2/cloud-record/share.html?record_id=xxx" --password 提取码
```
`fetch` 会尝试自动从分享页提取视频流并下载转写为 srt/txt。由于回放页是 JS 渲染、
播放流带时效 token，自动提取不一定成功——失败时助手会打印**分步引导**：浏览器 F12 →
Network → 筛选 `m3u8` → 复制流地址，然后：
```bash
python main.py run --input "https://.../xxx.m3u8?token=..." --title "课程名"
# 或只下载转写不整理：
python main.py fetch --input "https://.../xxx.m3u8?token=..."
```
下载优先调用本机 ffmpeg（已检测到你装有 8.1.2）；无 ffmpeg 时自动退回纯标准库 HLS 下载。

**路径 C（最轻量）：会议转写文稿**
老师若导出了「会议转写」（商业版功能），拿到 txt 直接 `run --input 文稿.txt`。

> 下载与整理仅限你合法拥有访问权限的课程回放，请遵守分享者设置的权限。

## 四、核心工作流（与你要求的流程一一对应）

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

## 五、配置

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
  }
}
```

API 密钥**只**从 `config.json`（不入库）或环境变量 `COURSE_AGENT_API_KEY` 读取，永远不会出现在代码与提交记录中。

## 六、“训练”：助手如何越用越好

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

## 七、测试

```bash
python -m unittest discover -s tests -v
```

106 个用例覆盖：JSON 容错提取、字幕/HTML 解析、分块完整性、HTTP 重试与回落、
计划/审查/测试各阶段校验、编排循环的失败恢复与终止保证、记忆库容量治理、CLI 端到端。
全部离线运行（Mock 模型），无需网络与本地模型。

## 八、工程保障（无逻辑错误 / 无内存泄漏）

- **循环必然终止**：外层 `max_attempts` 上限 + 内部重试上限，失败路径全部有出口
- **资源零堆积**：HTTP 用 `urllib` 每请求独立连接（用完即关）；文件全部 `with` 托管；
  sqlite 连接显式关闭；转写模型用完即释放
- **容量有上限**：范例库按分数滚动保留 200 条；反馈/会话表按主键去重；日志无全局累积
- **输入容错**：GBK/UTF-8 自动识别、残缺 HTML 容忍解析、模型输出围栏/思考段/杂文均能提取
- **异常有边界**：CLI 层统一捕获业务异常返回非零退出码，方便脚本化调用

## 九、目录结构

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

## 十、已知边界

- 实时直播中的**同步**字幕抓取因平台而异，本工具面向**课后材料**（录屏、字幕、文稿）整理
- 音视频转写首次使用需下载 whisper 模型权重（约 500MB，CPU 可跑）
- 忠实度抽查是抽样而非全量，极端情况下个别编造内容可能漏检——可用 `rate` 命令反馈，
  低分章节不会进入范例库
