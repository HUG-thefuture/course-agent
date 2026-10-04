"""SM-2 间隔复习 + 错题本 + 掌握度追踪（纯标准库，JSON 持久化）。

- SM-2：质量 0-5 打分（≥3 通过），间隔序列 1/3/7/14/30/60 天，
  按「答对且快 ×1.3、答对慢 ×1.0、答错重置 1 天」调整；
  连续答对 3 次晋升更高认知层级。
- 错题本：答错的题自动入册，复习时优先重现。
- 掌握度：按知识点累计 正确/尝试 与 Bloom 最高层级。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

INTERVALS = [1, 3, 7, 14, 30, 60]
_FAST = 1.3
_SLOW_LIMIT = 8.0  # 秒：超过视为"慢"


def _now() -> float:
    return time.time()


def sm2_next(card: dict, quality: int, seconds: float) -> dict:
    """按 SM-2 变体更新卡片。quality: 0-5（<3 视为答错）。返回更新后的卡片。"""
    q = max(0, min(5, quality))
    fast = seconds <= _SLOW_LIMIT
    if q < 3:
        card["interval"] = INTERVALS[0]
        card["reps"] = 0
        card["lapses"] = card.get("lapses", 0) + 1
        card["streak"] = 0
    else:
        streak = card.get("streak", 0) + 1
        idx = min(card.get("reps", 0), len(INTERVALS) - 1)
        base = INTERVALS[idx]
        factor = _FAST if (fast and q >= 4) else 1.0
        interval = max(1, round(base * factor))
        card["interval"] = interval
        card["reps"] = card.get("reps", 0) + 1
        card["streak"] = streak
        if streak >= 3:
            card["bloom"] = card.get("next_bloom") or card.get("bloom", "理解")
    card["ease"] = round(min(3.0, max(1.3, card.get("ease", 2.5) + (0.1 if q >= 4 else -0.2))), 2)
    card["due"] = _now() + card["interval"] * 86400
    card["last"] = _now()
    card["last_quality"] = q
    return card


class ReviewStore:
    """复习状态持久化：卡片（按知识点）+ 错题本。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.cards: dict[str, dict] = {}
        self.wrongbook: list[dict] = []
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.cards = d.get("cards", {})
                self.wrongbook = d.get("wrongbook", [])
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"cards": self.cards, "wrongbook": self.wrongbook},
            ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------ 记录
    def record(self, kp_id: str, kp_name: str, question: str, user_answer: str,
               correct: bool, seconds: float = 5.0, source: str = "") -> dict:
        """记录一次作答：更新 SM-2 卡片；答错入错题本。返回更新后的卡片。"""
        card = self.cards.get(kp_id, {
            "kp_id": kp_id, "kp_name": kp_name, "interval": 1, "reps": 0,
            "lapses": 0, "streak": 0, "ease": 2.5, "bloom": "记忆",
            "next_bloom": "理解"})
        q = 5 if correct else 2
        card = sm2_next(card, q, seconds)
        card["kp_name"] = kp_name
        self.cards[kp_id] = card
        if not correct:
            self.wrongbook.append({
                "time": time.strftime("%Y-%m-%d %H:%M"), "kp_id": kp_id,
                "kp_name": kp_name, "question": question[:300],
                "user_answer": str(user_answer)[:300], "source": source[:120],
                "resolved": False})
            self.wrongbook = self.wrongbook[-500:]  # 上限防膨胀
        return card

    # ------------------------------------------------------------ 查询
    def due_cards(self, k: int = 10) -> list[dict]:
        now = _now()
        due = [c for c in self.cards.values() if c.get("due", 0) <= now]
        due.sort(key=lambda c: c.get("due", 0))
        return due[:k]

    def mastery(self, learned_threshold: int = 3) -> dict[str, dict]:
        """按知识点掌握度：reps≥阈值且 streak≥2 记为已掌握。"""
        out = {}
        for kp_id, c in self.cards.items():
            out[kp_id] = {
                "name": c.get("kp_name", kp_id),
                "mastered": c.get("reps", 0) >= learned_threshold
                and c.get("streak", 0) >= 2,
                "reps": c.get("reps", 0), "lapses": c.get("lapses", 0),
                "interval": c.get("interval", 1),
                "next_bloom": c.get("next_bloom", "理解")}
        return out

    def unresolved_wrongbook(self) -> list[dict]:
        return [w for w in self.wrongbook if not w.get("resolved")]

    def mark_wrong_resolved(self, index: int) -> None:
        if 0 <= index < len(self.wrongbook):
            self.wrongbook[index]["resolved"] = True
