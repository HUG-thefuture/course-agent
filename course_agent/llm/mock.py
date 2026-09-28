"""离线 Mock 模型：确定性生成合法输出。

用途：
1. 单元测试与 CI：无需网络、无需本地模型即可验证整条管线；
2. `demo` 命令：让用户在配置模型前先看到完整产物样例。

通过提示词中的任务标记（TASK: XXX）与【字段】提取输入信息。
"""

from __future__ import annotations

import json
import re

from ..utils import extract_json
from .base import BaseLLM

_TASK_RE = re.compile(r"TASK:\s*(\w+)")


def _field(user: str, name: str) -> str:
    m = re.search(rf"【{name}】(.*)", user)
    return m.group(1).strip() if m else ""


def _block(user: str, name: str) -> str:
    m = re.search(rf"【{name}】\n(.*?)(?=\n【|$)", user, re.S)
    return m.group(1).strip() if m else ""


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?\n]+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 8]


class MockLLM(BaseLLM):
    def __init__(self, fail_plan: int = 0, fail_review: int = 0,
                 fail_notes: int = 0, fail_qa: int = 0,
                 fail_coding: int = 0) -> None:
        self.fail_plan = fail_plan
        self.fail_review = fail_review
        self.fail_notes = fail_notes
        self.fail_qa = fail_qa
        self.fail_coding = fail_coding
        self.calls = 0

    @property
    def name(self) -> str:
        return "mock"

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 2048) -> str:
        self.calls += 1
        # 任务标记与【字段】都在 system（渲染后的模板）里，合并后统一解析
        combined = (system or "") + "\n" + (user or "")
        m = _TASK_RE.search(system or "") or _TASK_RE.search(combined)
        task = m.group(1) if m else "THINK"
        handler = {
            "THINK": self._think,
            "PLAN": self._plan,
            "REVIEW": self._review,
            "NOTES": self._notes,
            "EXERCISES": self._exercises,
            "CODING": self._coding,
            "QA_JUDGE": self._qa,
        }.get(task, self._think)
        return handler(combined)

    # ------------------------------------------------------------------ tasks
    def _think(self, user: str) -> str:
        title = _field(user, "课程标题") or "未命名课程"
        n = _field(user, "分块数") or "?"
        return f"材料主题为《{title}》，共 {n} 个文本块。建议按块归纳章节，逐节提炼知识点、关键节点并配套习题。"

    def _plan(self, user: str) -> str:
        if self.fail_plan > 0:
            self.fail_plan -= 1
            return "（模拟故障：这不是合法 JSON）"
        title = _field(user, "课程标题") or "课程"
        previews = _block(user, "分块预览")
        ids = [int(x) for x in re.findall(r"^#(\d+)", previews, re.M)]
        if not ids:
            ids = [0]
        sections = []
        group = 2 if len(ids) > 4 else 1
        for i in range(0, len(ids), group):
            part = ids[i : i + group]
            sections.append({
                "title": f"第{len(sections) + 1}节 " + _field(user, "课程标题"),
                "goals": ["理解本节核心概念", "能复述关键流程", "完成配套习题"],
                "chunk_indices": part,
                "exercise_count": 4,
            })
        return json.dumps({"course_title": title, "sections": sections},
                          ensure_ascii=False)

    def _review(self, user: str) -> str:
        if self.fail_review > 0:
            self.fail_review -= 1
            return json.dumps(
                {"passed": False, "issues": ["模拟审查：习题数量偏少，请增加练习覆盖面"]},
                ensure_ascii=False)
        return json.dumps({"passed": True, "issues": []}, ensure_ascii=False)

    def _notes(self, user: str) -> str:
        if self.fail_notes > 0:
            self.fail_notes -= 1
            return "（模拟故障：坏输出）"
        title = _field(user, "章节标题") or "本节"
        has_ts = _field(user, "含时间戳").lower() == "true"
        start = _field(user, "起始时间")
        source = _block(user, "源文本")
        sents = _sentences(source) or [f"{title}的核心内容"]
        picked = sorted(sents, key=len, reverse=True)[:3]
        kps = []
        for i, s in enumerate(picked):
            kps.append({
                "title": f"{title}·要点{i + 1}",
                "detail": s,
                "time": start if (has_ts and i == 0) else "",
            })
        key_nodes = [
            {"label": "重点讲解开始", "why": picked[0][:40], "time": start if has_ts else ""},
            {"label": "本节小结", "why": "讲师对本节要点做归纳，适合回看", "time": ""},
        ]
        return json.dumps({"knowledge_points": kps, "key_nodes": key_nodes},
                          ensure_ascii=False)

    def _exercises(self, user: str) -> str:
        title = _field(user, "章节标题") or "本节"
        source = _block(user, "源文本")
        sents = _sentences(source) or [f"{title}的要点"]
        sents_sorted = sorted(sents, key=len, reverse=True)
        s1 = sents_sorted[0]
        s2 = sents_sorted[1] if len(sents_sorted) > 1 else s1
        s3 = sents_sorted[2] if len(sents_sorted) > 2 else s2

        m = re.search(r"[\u4e00-\u9fff]{4,8}", s3)
        blank_word = m.group(0) if m else s3[:4]
        blank_q = s3.replace(blank_word, "____", 1)
        if blank_q == s3:
            blank_q = f"本节提到：{s3[:30]}____"
            blank_word = s3[30:40] if len(s3) > 40 else "要点"

        exercises = [
            {
                "type": "single_choice",
                "question": f"关于「{title}」，以下哪项最符合课程内容？",
                "options": [s1[:60], "课程中明确否定的错误做法", "与课程主题无关的干扰项", "过于绝对化的错误表述"],
                "answer": "A",
                "explanation": f"课程原文：{s1[:80]}",
                "difficulty": "easy",
            },
            {
                "type": "true_false",
                "question": f"判断：{s2[:60]}",
                "answer": "对",
                "explanation": f"对应课程原文：{s2[:80]}",
                "difficulty": "easy",
            },
            {
                "type": "fill_blank",
                "question": blank_q,
                "answer": blank_word,
                "explanation": f"课程原文：{s3[:80]}",
                "difficulty": "medium",
            },
            {
                "type": "short_answer",
                "question": f"请简述「{title}」中关于“{s1[:12]}”的核心要点。",
                "answer": s2[:120],
                "explanation": "参考课程讲解内容作答即可。",
                "difficulty": "medium",
            },
        ]
        return json.dumps({"exercises": exercises}, ensure_ascii=False)

    def chat_with_image(self, system: str, user: str, images: list,
                        temperature: float = 0.3, max_tokens: int = 2048) -> str:
        """视觉接口的 Mock：按图片数量返回通用描述。"""
        n = len(images or [])
        rows = [{"idx": i,
                 "caption": f"屏幕演示{i + 1}：代码编辑器中展示组件实现，含函数定义与终端命令"}
                for i in range(n)]
        return json.dumps({"frames": rows}, ensure_ascii=False)

    def _coding(self, user: str) -> str:
        if self.fail_coding > 0:
            self.fail_coding -= 1
            return "（模拟故障：坏输出）"
        title = _field(user, "章节标题") or "本节"
        source = _block(user, "源文本")
        sents = _sentences(source) or [f"{title}的核心功能"]
        task = sorted(sents, key=len, reverse=True)[0]
        exercises = [
            {
                "type": "coding_practice",
                "question": f"编程实战：结合「{title}」的内容，动手实现课程中演示的功能——{task[:40]}。要求代码可运行，并处理课程中提到的边界情况。",
                "starter_code": "// TODO: 按课程讲解补全实现\nfunction solve() {\n  // your code here\n}\n",
                "answer": "// 参考实现（结合课程要点）\nfunction solve() {\n  // " + task[:60] + "\n  return 'ok';\n}\n",
                "hints": ["回顾课程中对该功能的分步讲解", "先跑通最小流程，再补边界情况"],
                "check_points": ["功能与课程演示一致", "处理了课程提到的易错点", "命名与结构清晰"],
                "explanation": f"本题对应课程原文：{task[:80]}",
                "difficulty": "medium",
            }
        ]
        return json.dumps({"exercises": exercises}, ensure_ascii=False)

    def _qa(self, user: str) -> str:
        items_block = _block(user, "条目")
        try:
            data = extract_json(items_block)  # 容忍块尾混入的指令文本
        except (ValueError, TypeError):
            data = []
        items = data if isinstance(data, list) else []
        if self.fail_qa > 0:
            self.fail_qa -= 1
            out = [{"idx": i, "supported": False, "reason": "模拟质检：证据不足"} for i in range(len(items))]
            return json.dumps({"items": out}, ensure_ascii=False)
        out = [{"idx": i, "supported": True, "reason": "与原文一致"} for i in range(len(items))]
        return json.dumps({"items": out}, ensure_ascii=False)
