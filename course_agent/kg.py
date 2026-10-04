"""知识图谱：知识点节点 + 六类关系边 + 前置校验 + 路径规划。

纯标准库实现（邻接表 + DFS 环检测 + 拓扑排序），JSON 持久化。
关系类型：prerequisite / successor / part_of / related_to /
confused_with / applies_to（前置边自动生成反向 successor 边）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

RELATIONS = ("prerequisite", "successor", "part_of", "related_to",
             "confused_with", "applies_to")


class KnowledgeGraph:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.nodes: dict[str, dict] = {}   # id -> {name, course, section, difficulty, bloom}
        self.edges: list[dict] = []        # {src, dst, rel}

    # ------------------------------------------------------------- load/save
    def load(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.nodes = d.get("nodes", {})
            self.edges = d.get("edges", [])
            return True
        except (json.JSONDecodeError, OSError):
            return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"nodes": self.nodes, "edges": self.edges},
            ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------- mutation
    def add_node(self, kp_id: str, name: str, course: str = "",
                 section: str = "", difficulty: int = 2,
                 bloom: str = "理解") -> None:
        self.nodes.setdefault(kp_id, {
            "name": name, "course": course, "section": section,
            "difficulty": difficulty, "bloom": bloom})

    def add_edge(self, src: str, dst: str, rel: str) -> None:
        if rel not in RELATIONS:
            raise ValueError(f"未知关系类型: {rel}")
        if src == dst:
            return
        edge = {"src": src, "dst": dst, "rel": rel}
        if edge not in self.edges:
            self.edges.append(edge)
            if rel == "prerequisite":  # 前置 A→B 蕴含 B 是 A 的后继
                rev = {"src": dst, "dst": src, "rel": "successor"}
                if rev not in self.edges:
                    self.edges.append(rev)

    # ------------------------------------------------------------- queries
    def _adj(self, rel: str, reverse: bool = False) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {}
        for e in self.edges:
            if e["rel"] != rel:
                continue
            s, d = (e["dst"], e["src"]) if reverse else (e["src"], e["dst"])
            adj.setdefault(s, []).append(d)
        return adj

    def neighbors(self, kp_id: str, rel: str) -> list[str]:
        return self._adj(rel).get(kp_id, [])

    def prereq_closure(self, kp_id: str) -> set[str]:
        """kp 的全部传递前置（DFS，含去重）。"""
        seen: set[str] = set()
        stack = list(self.neighbors(kp_id, "prerequisite"))
        while stack:
            x = stack.pop()
            if x in seen or x == kp_id:
                continue
            seen.add(x)
            stack.extend(self.neighbors(x, "prerequisite"))
        return seen

    def find_cycles(self) -> list[list[str]]:
        """前置关系成环检测（DFS 三色标记）。"""
        adj = self._adj("prerequisite")
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self.nodes}
        for e in self.edges:
            color.setdefault(e["src"], WHITE)
            color.setdefault(e["dst"], WHITE)
        cycles: list[list[str]] = []
        stack: list[str] = []

        def dfs(u: str) -> None:
            color[u] = GRAY
            stack.append(u)
            for v in adj.get(u, []):
                if color.get(v, BLACK) == GRAY:
                    i = stack.index(v)
                    cycles.append(stack[i:] + [v])
                elif color.get(v, WHITE) == WHITE:
                    dfs(v)
            stack.pop()
            color[u] = BLACK

        for n in list(color):
            if color[n] == WHITE:
                dfs(n)
        return cycles

    def topo_order(self, ids: Optional[set[str]] = None,
                   roots: Optional[list[str]] = None) -> list[str]:
        """目标集合的拓扑学习顺序（Kahn）。roots 缺省用无前置的节点。"""
        ids = set(ids) if ids else set(self.nodes)
        adj = self._adj("prerequisite")
        indeg = {i: 0 for i in ids}
        for i in ids:
            for p in adj.get(i, []):  # p 是 i 的前置
                if p in ids:
                    indeg[i] += 1
        if roots:
            queue = [r for r in roots if r in ids]
        else:
            queue = sorted([i for i in ids if indeg[i] == 0])
        order: list[str] = []
        while queue:
            u = queue.pop(0)
            if u not in ids:
                continue
            order.append(u)
            # 松弛：u 完成后，所有以 u 为前置的节点入度-1
            for v in ids:
                if u in adj.get(v, []) and v in indeg and indeg[v] > 0:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        queue.append(v)
        if len(order) != len(ids):  # 有环：剩余节点按字母补齐并标注
            rest = sorted(ids - set(order))
            order += rest
        return order

    def check_path(self, path: list[str]) -> list[str]:
        """校验学习路径：返回违反前置关系的警告列表。"""
        warns = []
        learned: set[str] = set()
        for kp in path:
            for p in self.neighbors(kp, "prerequisite"):
                if p not in learned and p in set(path):
                    warns.append(f"{kp} 的前置 {p} 未先学")
            learned.add(kp)
        return warns

    def missing_prereq(self, kp_id: str, learned: set[str]) -> list[str]:
        return [p for p in self.neighbors(kp_id, "prerequisite")
                if p not in learned]

    # ------------------------------------------------------------- planning
    def recommend_next(self, learned: set[str], k: int = 3) -> list[dict]:
        """推荐下一步可学的知识点：前置已满足、按已学关联度排序。"""
        cand = []
        for nid, meta in self.nodes.items():
            if nid in learned:
                continue
            missing = self.missing_prereq(nid, learned)
            if missing:
                continue
            link = len(set(self.neighbors(nid, "related_to"))
                       & learned) + len(set(self.neighbors(nid, "applies_to"))
                                        & learned)
            cand.append({"kp_id": nid, "name": meta.get("name", nid),
                         "course": meta.get("course", ""), "links": link,
                         "difficulty": meta.get("difficulty", 2)})
        cand.sort(key=lambda x: (-x["links"], x["difficulty"]))
        return cand[:k]
