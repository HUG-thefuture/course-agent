"""持久记忆库（sqlite）：会话记录、高质量范例（少样本自改进）、用户反馈。

容量治理：范例表按分数保留上限条数，防止无限增长。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .utils import similarity

_log = logging.getLogger(__name__)

MAX_EXAMPLES = 200


class Memory:
    def __init__(self, db_path: str | Path) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(p))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    score REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS examples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    section_title TEXT NOT NULL,
                    source_excerpt TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    section_title TEXT NOT NULL DEFAULT '',
                    score INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------- sessions
    def save_session(self, session_id: str, title: str, source: str,
                     passed: bool, score: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)",
                (session_id, title, source, int(passed), score,
                 time.strftime("%Y-%m-%d %H:%M:%S")))

    # ------------------------------------------------------------- examples
    def add_example(self, session_id: str, section_title: str,
                    source_excerpt: str, output_json: str, score: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO examples (session_id, section_title, source_excerpt,"
                " output_json, score, created_at) VALUES (?,?,?,?,?,?)",
                (session_id, section_title, source_excerpt, output_json, score,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self._conn.execute(
                """
                DELETE FROM examples WHERE id NOT IN (
                    SELECT id FROM examples ORDER BY score DESC, id DESC LIMIT ?
                )
                """,
                (MAX_EXAMPLES,))

    def recall_examples(self, query: str, k: int = 2) -> list[dict[str, Any]]:
        """按相似度召回高质量范例；无相关内容时返回空（避免噪声少样本）。"""
        rows = self._conn.execute(
            "SELECT section_title, source_excerpt, output_json, score "
            "FROM examples ORDER BY score DESC, id DESC LIMIT ?",
            (MAX_EXAMPLES,)).fetchall()
        scored = []
        for r in rows:
            sim = similarity(query, r["section_title"] + " " + r["source_excerpt"])
            if sim > 0.05:
                scored.append((sim, dict(r)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:k]]

    def count_examples(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM examples").fetchone()[0])

    def all_examples(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT section_title, source_excerpt, output_json, score "
            "FROM examples ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- feedback
    def add_feedback(self, session_id: str, section_title: str,
                     score: int, note: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO feedback (session_id, section_title, score, note, created_at)"
                " VALUES (?,?,?,?,?)",
                (session_id, section_title, score, note,
                 time.strftime("%Y-%m-%d %H:%M:%S")))

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
