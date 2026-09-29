"""管线各阶段共享的数据模型（dataclass）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Segment:
    """一段素材：start 为相对起点秒数（无时间戳素材为 None）。"""

    start: Optional[float]
    text: str


@dataclass
class Material:
    """加载后的课程素材。kind: text/srt/vtt/pdf/audio/video/url。"""

    title: str
    kind: str
    source: str
    segments: list[Segment] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(s.text) for s in self.segments)

    @property
    def has_timestamps(self) -> bool:
        return any(s.start is not None for s in self.segments)


@dataclass
class Chunk:
    index: int
    text: str
    start_time: Optional[float] = None
    end_time: Optional[float] = None


@dataclass
class PlanSection:
    title: str
    goals: list[str] = field(default_factory=list)
    chunk_indices: list[int] = field(default_factory=list)
    exercise_count: int = 4


@dataclass
class Plan:
    course_title: str
    sections: list[PlanSection] = field(default_factory=list)


@dataclass
class Review:
    passed: bool
    issues: list[str] = field(default_factory=list)


@dataclass
class KnowledgePoint:
    title: str
    detail: str
    time: str = ""
    concept: str = ""   # 完整概念讲解（复习主文档用）
    example: str = ""   # 代码/操作示例
    pitfall: str = ""   # 易错点/常见误区


@dataclass
class Expansion:
    """相关性拓展：课程内容之外、但强相关的知识补充（明确标注为拓展）。"""

    topic: str
    content: str


@dataclass
class KeyNode:
    label: str
    why: str
    time: str = ""


@dataclass
class Exercise:
    type: str  # single_choice / multi_choice / true_false / fill_blank / short_answer / coding_practice
    question: str
    answer: str
    explanation: str = ""
    options: list[str] = field(default_factory=list)
    difficulty: str = "medium"
    starter_code: str = ""          # 编程题：代码骨架（含 TODO）
    hints: list[str] = field(default_factory=list)          # 编程题：思路提示
    check_points: list[str] = field(default_factory=list)   # 编程题：验收要点
    language: str = ""              # 编程题：python / javascript / other
    tests: list[str] = field(default_factory=list)          # 编程题：可执行测试断言

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.type,
            "question": self.question,
            "answer": self.answer,
            "explanation": self.explanation,
            "difficulty": self.difficulty,
        }
        if self.type in ("single_choice", "multi_choice"):
            d["options"] = list(self.options)
        if self.type == "coding_practice":
            if self.starter_code:
                d["starter_code"] = self.starter_code
            if self.hints:
                d["hints"] = list(self.hints)
            if self.check_points:
                d["check_points"] = list(self.check_points)
            if self.language:
                d["language"] = self.language
            if self.tests:
                d["tests"] = list(self.tests)
        return d


@dataclass
class SectionOutput:
    title: str
    goals: list[str] = field(default_factory=list)
    knowledge_points: list[KnowledgePoint] = field(default_factory=list)
    key_nodes: list[KeyNode] = field(default_factory=list)
    exercises: list[Exercise] = field(default_factory=list)
    source_excerpt: str = ""  # 源文本摘要，用于范例沉淀
    source_text: str = ""     # 完整源文本，供忠实度抽查对照证据
    expansions: list[Expansion] = field(default_factory=list)  # 相关性拓展

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "goals": list(self.goals),
            "knowledge_points": [
                {"title": k.title, "detail": k.detail, "time": k.time,
                 "concept": k.concept, "example": k.example, "pitfall": k.pitfall}
                for k in self.knowledge_points
            ],
            "key_nodes": [
                {"label": n.label, "why": n.why, "time": n.time} for n in self.key_nodes
            ],
            "expansions": [
                {"topic": e.topic, "content": e.content} for e in self.expansions
            ],
            "exercises": [e.to_dict() for e in self.exercises],
        }


@dataclass
class QAReport:
    passed: bool
    issues: list[str] = field(default_factory=list)
    structural_issues: list[str] = field(default_factory=list)
    fidelity_ratio: float = 1.0
    score: int = 0


@dataclass
class Frame:
    """视频关键帧：截图路径 + 对应时间（秒）+ 可选视觉描述。"""

    path: str
    time: float
    caption: str = ""


@dataclass
class AttemptRecord:
    index: int
    review_passed: bool = False
    review_issues: list[str] = field(default_factory=list)
    qa_passed: bool = False
    qa_issues: list[str] = field(default_factory=list)


@dataclass
class SessionResult:
    session_id: str
    title: str
    source: str
    plan: Plan
    sections: list[SectionOutput] = field(default_factory=list)
    qa: Optional[QAReport] = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    passed: bool = False
    output_dir: str = ""
    frames: list[Frame] = field(default_factory=list)
    partial: bool = False   # 素材是否按"不完整课程"处理

    def to_summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "source": self.source,
            "passed": self.passed,
            "qa_score": self.qa.score if self.qa else 0,
            "sections": len(self.sections),
            "exercises": sum(len(s.exercises) for s in self.sections),
            "attempts": len(self.attempts),
            "output_dir": self.output_dir,
            "partial": self.partial,
            "frames": len(self.frames),
        }
