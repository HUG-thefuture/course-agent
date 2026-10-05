"""双擎学习助手 Agent 核心：七分支意图路由 + 双擎规范输出。

分支：A 知识点讲解 / B 习题生成 / C 薄弱点诊断 / D 体系梳理 /
E 学习路径 / F 答疑纠错 / G 跨课程串联。
检索走混合知识库（向量+BM25+RRF），路径与诊断走知识图谱，
复习调度走 SM-2。输出遵循「双擎学习助手」输出规范。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .kg import KnowledgeGraph
from .kb_vector import OllamaEmbedder, hybrid_search
from .kb import KnowledgeBase
from .review import ReviewStore
from .utils import truncate


class TutorAgent:
    def __init__(self, llm, course_root: str | Path = "output/特训营全部课程",
                 data_dir: str | Path = "data"):
        self.llm = llm
        self.course_root = Path(course_root)
        self.data_dir = Path(data_dir)
        self.kb = KnowledgeBase(self.data_dir / "kb_index.json")
        self.kb.load()
        self.embedder = OllamaEmbedder()
        self.chroma_dir = self.data_dir / "chromadb"
        self.kg = KnowledgeGraph(self.data_dir / "kg.json")
        self.kg.load()
        self.review = ReviewStore(self.data_dir / "review_state.json")
        self.learned: set[str] = set()

    # ------------------------------------------------------------ 检索
    def _search(self, query: str, top_k: int = 6,
                type_: Optional[str] = None) -> list[dict]:
        try:
            return hybrid_search(self.kb, self.chroma_dir, self.embedder,
                                 query, top_k=top_k, type_=type_)
        except Exception:
            return self.kb.search(query, top_k=top_k, type_=type_)

    # ------------------------------------------------------------ 意图
    def classify(self, message: str) -> tuple[str, str]:
        """关键词路由。返回 (分支, 主题)。"""
        m = message
        rules = [
            ("G", ("跨课程", "课程之间", "课程间", "多门课程", "几门课",
                   "所有课程", "课程整合", "整合复习"), False),
            ("C", ("不太会", "不会", "卡在", "薄弱", "查漏"), False),
            ("E", ("学习路径", "怎么开始", "怎么学", "规划", "从零", "路线"), False),
            ("D", ("体系", "知识地图", "什么关系", "串联"), False),
            ("B", ("出题", "给我几道", "练习", "考考我", "习题"), False),
            ("F", ("为什么报错", "报错", "为什么错", "哪里错了", "排错"), False),
            ("A", ("讲讲", "讲一下", "什么是", "教我", "学学", "详解"), False),
        ]
        for branch, kws, need_course in rules:
            for kw in kws:
                if kw in m and (not need_course or "课程" in m):
                    topic = m.replace(kw, " ").strip() or m
                    return branch, topic
        return "A", m

    # ------------------------------------------------------------ 工具
    def _llm(self, system: str, user: str, max_tokens: int = 3072,
             temperature: float = 0.4) -> str:
        return self.llm.chat(system, user, temperature=temperature,
                             max_tokens=max_tokens)

    def _search_block(self, rows: list[dict]) -> str:
        parts = []
        for r in rows[:6]:
            parts.append(f"【{truncate(r.get('course', ''), 24)} · "
                         f"{truncate(r.get('section', ''), 20)}"
                         f"{' · ' + r['time'] if r.get('time') else ''}】\n"
                         f"{truncate(r.get('concept') or r.get('text', ''), 420)}")
        return "\n\n".join(parts)

    def _citations(self, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows[:6]:
            out.append({"course": truncate(r.get("course", ""), 30),
                        "section": truncate(r.get("section", ""), 24),
                        "time": r.get("time", ""),
                        "type": r.get("type", "")})
        return out

    # ------------------------------------------------------------ 分支实现
    def explain(self, topic: str) -> dict:
        rows = self._search(topic)
        if not rows:
            return {"branch": "A", "text": f"知识库中未找到与「{topic}」相关的课程内容。"
                    "我可以补充通用说明（会标注「非课程内容」），或你换个说法再试。"}
        ctx = self._search_block(rows)
        system = (
            "你是双擎学习助手的讲解模块。按以下结构输出（Markdown）：\n"
            "## 知识点：<名称>\n**来源：**<课程·章节·时间>\n**难度：**⭐x/5\n"
            "**前置知识：**...\n**认知层级：**...\n### 一句话定义\n...\n"
            "### 为什么需要它\n...\n### 核心内容\n(分点+示例)\n"
            "### 代码示例\n(完整可运行)\n### 常见误区\n(2-3个)\n"
            "### 与相邻知识点的关系\n(前置/后继/易混淆)\n### 自测\n(1-2题, 答案折叠标注)\n\n"
            "纪律：只依据【知识库片段】；补充课程外知识须标注「（补充，非课程内容）」；"
            "片段不足时明确说明。\n\n"
            f"【主题】{topic}\n【知识库片段】\n{ctx}")
        text = self._llm(system, f"请讲解「{topic}」。")
        return {"branch": "A", "text": text,
                "citations": self._citations(rows)}

    def gen_exercises(self, topic: str, n: int = 4,
                      want_coding: bool = True) -> dict:
        rows = self._search(topic, type_="knowledge", top_k=4)
        ctx = self._search_block(rows) or truncate(topic, 100)
        system = (
            "你是双擎学习助手的出题模块。基于【知识库片段】出 "
            f"{n} 道题，覆盖至少 2 种题型、难度递进（easy→hard）；"
            "知识点适合编程时至少 1 道编程题。\n"
            "编程题要素：题名/难度(⭐1-5)/知识点/来源/背景/任务描述/"
            "输入输出格式/2个样例/约束/3级提示/完整可运行参考实现(带注释)/"
            "常见错误2条/关联知识点。函数名在题干中明确规定。\n"
            "客观题给 options(单选4个)或 answer(判断:对/错)与 explanation。\n"
            "纪律：题目仅来自【知识库片段】；干扰项对应真实误区；提示不泄露答案。\n"
            '只输出 JSON：{"exercises": [{"kind": "single_choice|true_false|'
            'fill_blank|short_answer|coding", "difficulty": "⭐x", "question": "", '
            '"options": [], "answer": "", "explanation": "", '
            '"starter_code": "", "tests": ["assert ..."], "hints": [], '
            '"check_points": [], "reference_solution": "", "kp": ""}]}\n\n'
            f"【主题】{topic}\n【知识库片段】\n{ctx}")
        from .utils import extract_json
        raw = self._llm(system, f"请出 {n} 道题。", max_tokens=4096)
        try:
            data = extract_json(raw)
            exs = data.get("exercises") or []
        except (ValueError, TypeError):
            exs = []
        return {"branch": "B", "exercises": exs[:n],
                "citations": self._citations(rows),
                "note": "出题完成。请逐题作答（编程题粘贴代码可点在线测试）；"
                        "交卷后我出解析与强化知识。"}

    def diagnose(self, topic: str) -> dict:
        rows = self._search(topic, top_k=6)
        ctx = self._search_block(rows)
        system = (
            "你是双擎学习助手的诊断模块。先基于【知识库片段】出 3-5 道覆盖该范围的"
            "诊断题（含答案），再按以下结构输出：\n"
            "## 诊断题\n(逐题)\n## 诊断结果\n(等用户作答后填充——现在先给出"
            "**待诊断**占位)\n## 知识点清单\n(本范围知识点列表, 供定位)\n\n"
            f"【范围】{topic}\n【知识库片段】\n{ctx}")
        text = self._llm(system, "请生成诊断题。")
        return {"branch": "C", "text": text, "citations": self._citations(rows)}

    def system_map(self, scope: str) -> dict:
        rows = self._search(scope, top_k=10)
        # 图谱关系（若已抽取）
        kg_lines = []
        for e in self.kg.edges[:20]:
            n1 = self.kg.nodes.get(e["src"], {}).get("name", e["src"])
            n2 = self.kg.nodes.get(e["dst"], {}).get("name", e["dst"])
            kg_lines.append(f"- {n1} --[{e['rel']}]--> {n2}")
        ctx = self._search_block(rows)
        system = (
            "你是双擎学习助手的体系梳理模块。基于【知识库片段】输出：\n"
            "## 知识体系：<范围>\n### 全景图\n(文字树状图)\n### 依赖关系\n"
            "(哪些必须先学/可并行)\n### 核心主线\n(3-5个知识点串主线)\n"
            "### 易混淆点对照表\n(Markdown表格: A|B|区别|记忆方法)\n"
            "### 掌握度自检\n(逐知识点检查项)\n\n"
            f"【范围】{scope}\n【图谱关系(供参考)】\n" + "\n".join(kg_lines)
            + f"\n【知识库片段】\n{ctx}")
        text = self._llm(system, "请梳理体系。", max_tokens=3072)
        return {"branch": "D", "text": text, "citations": self._citations(rows)}

    def learning_path(self, goal: str, days: int = 14) -> dict:
        rows = self._search(goal, top_k=10)
        # 图谱辅助：推荐下一步 + 前置校验
        learned = self.learned
        recs = self.kg.recommend_next(learned, k=5) if self.kg.nodes else []
        kg_hint = "\n".join(f"- {r['name']}（来源 {r.get('course', '')}）"
                            for r in recs) or "（图谱尚未抽取，按知识库排序）"
        ctx = self._search_block(rows)
        system = (
            "你是双擎学习助手的路径规划模块。基于【知识库片段】生成路径：\n"
            "## 学习路径：<目标>\n**预计周期：**(按每天2小时)\n**前置要求：**\n"
            "### 阶段一~N：(每阶段: 目标/知识点(拓扑排序,标注⭐难度与来源)/"
            "练习3-5题含编程/验收标准)\n### 里程碑检查点\n### 推荐节奏\n"
            "纪律：不得推荐未满足前置的知识点；若用户跳学主动提醒。\n\n"
            f"【目标】{goal}（可用时长 {days} 天）\n"
            f"【图谱推荐下一步】\n{kg_hint}\n【知识库片段】\n{ctx}")
        text = self._llm(system, "请生成学习路径。", max_tokens=4096)
        return {"branch": "E", "text": text, "citations": self._citations(rows),
                "kg_recommend": recs}

    def qa_debug(self, message: str) -> dict:
        rows = self._search(message, top_k=5)
        ctx = self._search_block(rows)
        system = (
            "你是双擎学习助手的答疑模块。步骤：1复述问题确认理解 2检索到的"
            "相关知识点 3定位错误原因(概念/语法/逻辑/环境) 4给出修正代码"
            "(完整可运行)与解释 5关联知识点建议复习。只依据【知识库片段】，"
            "不足时说明。\n\n"
            f"【问题】{message}\n【知识库片段】\n{ctx}")
        text = self._llm(system, "请答疑。")
        return {"branch": "F", "text": text, "citations": self._citations(rows)}

    def cross_course(self, topic: str) -> dict:
        rows = self._search(topic, top_k=12)
        system = (
            "你是双擎学习助手的跨课程串联模块。基于【知识库片段】（来自多门课程）"
            "输出：\n## 跨课程串联\n| 知识点 | 课程A位置 | 课程B位置 | 差异 | 建议 |\n"
            "### 推荐学习顺序\n### 重复内容提示\n只使用片段中出现的课程与知识点。\n\n"
            f"【主题】{topic}\n【知识库片段】\n"
            + "\n\n".join(f"【{truncate(r.get('course', ''), 30)} · "
                          f"{truncate(r.get('section', ''), 20)}】\n"
                          f"{truncate(r.get('concept') or r.get('text', ''), 400)}"
                          for r in rows[:10]))
        text = self._llm(system, "请串联。", max_tokens=3072)
        return {"branch": "G", "text": text, "citations": self._citations(rows)}

    # ------------------------------------------------------------ 总入口
    def handle(self, message: str) -> dict:
        branch, topic = self.classify(message)
        table = {"A": self.explain, "B": self.gen_exercises, "C": self.diagnose,
                 "D": self.system_map, "E": self.learning_path,
                 "F": self.qa_debug, "G": self.cross_course}
        fn = table.get(branch, self.explain)
        kwargs = {}
        if branch == "E":
            kwargs["days"] = 14
        out = fn(topic, **kwargs)
        out["citations"] = out.get("citations") or []
        return out
