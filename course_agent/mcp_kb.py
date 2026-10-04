"""CourseAgent 知识库 MCP 服务器（stdio）。

供各类 agent 挂载（ZCode/Claude Code/Cursor 等）：
  {"mcpServers": {"course-agent-kb": {
      "command": "C:/Python314/python.exe",
      "args": ["-m", "course_agent.mcp_kb",
               "--course-root", "output/特训营全部课程"]}}}

工具：
  search_knowledge          关键词检索知识库
  ask_knowledge             检索+大模型生成带引用的回答
  rebuild_knowledge_base    重建索引（课程更新后调用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .kb import KnowledgeBase, build_kb, iter_course_chunks

INDEX_NAME = "kb_index.json"


class KBRuntime:
    def __init__(self, course_root: Path, data_dir: Path):
        from .kb_vector import OllamaEmbedder
        self.course_root = course_root
        self.data_dir = data_dir
        self.index_path = data_dir / INDEX_NAME
        self.chroma_dir = data_dir / "chromadb"
        self.embedder = OllamaEmbedder()
        self.kb = KnowledgeBase(self.index_path)
        if not self.kb.load():
            self.rebuild({})

    def rebuild(self, _args: dict) -> str:
        kb = build_kb(self.course_root, self.index_path)
        self.kb = kb
        return json.dumps({"status": "ok", "chunks": len(kb.chunks)},
                          ensure_ascii=False)

    def search(self, args: dict) -> str:
        from .kb_vector import hybrid_search
        if not self.kb.chunks and not self.kb.load():
            self.rebuild({})
        rows = hybrid_search(self.kb, self.chroma_dir, self.embedder,
                             str(args.get("query", "")),
                             top_k=int(args.get("top_k", 5)),
                             type_=args.get("type") or None)
        return json.dumps({"count": len(rows), "results": rows},
                          ensure_ascii=False, indent=1)

    def ask(self, args: dict) -> str:
        from .config import load_config
        from .llm.factory import build_client_chains
        from .utils import truncate

        question = str(args.get("question", "")).strip()
        if not question:
            return json.dumps({"error": "question 必填"}, ensure_ascii=False)
        from .kb_vector import hybrid_search
        if not self.kb.chunks and not self.kb.load():
            self.rebuild({})
        rows = hybrid_search(self.kb, self.chroma_dir, self.embedder,
                             question, top_k=int(args.get("top_k", 5)))
        context = "\n\n".join(
            f"【{r['course']} · {r['section']}】{r.get('title', '')}\n{truncate(r.get('concept') or r.get('text', ''), 500)}"
            for r in rows)
        answer = ""
        try:
            cfg = load_config()
            local_chain, cloud_chain = build_client_chains(cfg)
            from .llm import Router
            llm = Router(local_chain + cloud_chain)
            answer = llm.chat(
                "你是课程助教。仅依据【知识库片段】回答问题；片段不足以回答时明确说明。"
                "回答末尾标注引用来源（课程·章节）。\n"
                f"【问题】{question}\n【知识库片段】\n{truncate(context, 5000)}",
                "请作答。", max_tokens=2048)
        except Exception as e:  # 大模型不可用时退化为纯检索
            return json.dumps({"answer": "", "sources": rows[:5],
                               "error": f"生成回答失败（已附检索结果）: {e}"},
                              ensure_ascii=False)
        return json.dumps({"answer": answer, "sources": rows[:5]},
                          ensure_ascii=False)


TOOLS = [
    {"name": "search_knowledge",
     "description": "在课程知识库中检索知识点/习题/拓展（支持中文关键词）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "检索关键词"},
         "top_k": {"type": "integer", "description": "返回条数，默认 5"},
         "type": {"type": "string", "enum": ["knowledge", "expansion", "exercise"],
                  "description": "限定类型"}},
         "required": ["query"]}},
    {"name": "ask_knowledge",
     "description": "检索知识库并由大模型生成带引用的回答",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"}, "top_k": {"type": "integer"}},
         "required": ["question"]}},
    {"name": "rebuild_knowledge_base",
     "description": "课程更新后重建知识库索引",
     "inputSchema": {"type": "object", "properties": {}}},
]


def handle_tool(rt: KBRuntime, name: str, args: dict) -> dict:
    if name == "search_knowledge":
        return {"content": [{"type": "text", "text": rt.search(args)}]}
    if name == "ask_knowledge":
        return {"content": [{"type": "text", "text": rt.ask(args)}]}
    if name == "rebuild_knowledge_base":
        return {"content": [{"type": "text", "text": rt.rebuild(args)}]}
    return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CourseAgent 知识库 MCP 服务器 (stdio)")
    ap.add_argument("--course-root", default="output/特训营全部课程")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args(argv)

    rt = KBRuntime(Path(args.course_root), Path(args.data_dir))
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        if method == "initialize":
            result = {"protocolVersion": req.get("params", {}).get(
                "protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "course-agent-kb", "version": "1.0.0"}}
        elif method == "notifications/initialized" or method.startswith("notifications/"):
            continue
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = req.get("params") or {}
            result = handle_tool(rt, params.get("name", ""),
                                 params.get("arguments") or {})
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            if rid is not None:
                out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32601, "message": f"method not found: {method}"}},
                    ensure_ascii=False) + "\n")
            out.flush()
            continue
        if rid is not None:
            out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result},
                                 ensure_ascii=False) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
