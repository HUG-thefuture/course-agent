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


@dataclass
class KeyNode:
    label: str
    why: str
    time: str = ""


@dataclass
class Exercise:
    type: str  # single_choice / multi_choice / true_false / fill_blank / short_answer
    question: str
    answer: str
    explanation: str = ""
    options: list[str] = field(default_factory=list)
    difficulty: str = "medium"

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
        return d


@dataclass
class SectionOutput:
    title: str
    goals: list[str] = field(default_factory=list)
    knowledge_points: list[KnowledgePoint] = field(default_factory=list)
    key_nodes: list[KeyNode] = field(default_factory=list)
    exercises: list[Exercise] = field(default_factory=list)
    source_excerpt: str = ""  # 源文本摘要，用于质检对照与范例沉淀

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "goals": list(self.goals),
            "knowledge_points": [
                {"title": k.title, "detail": k.detail, "time": k.time}
                for k in self.knowledge_points
            ],
            "key_nodes": [
                {"label": n.label, "why": n.why, "time": n.time} for n in self.key_nodes
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
        }
