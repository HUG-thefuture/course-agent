"""MCP 知识库服务器：JSON-RPC stdio 往返 + 工具调用。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestMCPServer(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        # 造一个课程目录 + 索引
        course = self.td / "courses" / "01-demo课"
        course.mkdir(parents=True)
        (course / "session.json").write_text(json.dumps(
            {"title": "演示课程", "passed": True, "qa_score": 90,
             "sections": 1, "exercises": 1}), encoding="utf-8")
        (course / "sections.json").write_text(json.dumps([{
            "title": "事务基础",
            "goals": [],
            "knowledge_points": [{"title": "乐观锁", "detail": "通过version字段实现",
                                  "concept": "乐观锁靠版本号", "example": "",
                                  "pitfall": "忘比较version", "time": "00:10"}],
            "key_nodes": [],
            "expansions": [{"topic": "分布式锁", "content": "多节点互斥"}],
            "exercises": [{"type": "single_choice", "question": "乐观锁依赖什么？",
                           "answer": "A", "options": ["版本号", "时间戳"],
                           "explanation": "", "difficulty": "easy"}],
        }], ensure_ascii=False), encoding="utf-8")
        from course_agent.kb import build_kb
        self.kb = build_kb(self.td / "courses", self.td / "kb_index.json")
        assert self.kb.chunks

        self.proc = subprocess.Popen(
            [sys.executable, "-m", "course_agent.mcp_kb",
             "--course-root", str(self.td / "courses"),
             "--data-dir", str(self.td)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=str(ROOT))

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self._td.cleanup()

    def rpc(self, obj: dict) -> dict:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout
        line = self.proc.stdout.readline()
        return json.loads(line)

    def notify(self, obj: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def test_initialize_tools_list(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "course-agent-kb")
        self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names, ["search_knowledge", "ask_knowledge",
                                 "rebuild_knowledge_base"])

    def test_search_tool(self):
        self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_knowledge",
                                 "arguments": {"query": "乐观锁 version"}}})
        text = r["result"]["content"][0]["text"]
        d = json.loads(text)
        self.assertGreaterEqual(d["count"], 1)
        self.assertIn("乐观锁", json.dumps(d["results"], ensure_ascii=False))

    def test_rebuild_tool(self):
        self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "rebuild_knowledge_base", "arguments": {}}})
        d = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(d["status"], "ok")

    def test_survives_batch_array_and_null_params(self):
        # batch 数组请求 / params=null 不得杀掉服务器（旧实现直接 AttributeError 退出）
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(
            [{"jsonrpc": "2.0", "id": 7, "method": "ping"},
             {"jsonrpc": "2.0", "id": 8, "method": "ping"}]) + "\n")
        self.proc.stdin.flush()
        r1 = json.loads(self.proc.stdout.readline())
        r2 = json.loads(self.proc.stdout.readline())
        self.assertEqual(r1["id"], 7)
        self.assertEqual(r2["id"], 8)
        r = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "initialize",
                      "params": None})
        self.assertEqual(r["result"]["serverInfo"]["name"], "course-agent-kb")
        # 服务器仍存活
        r = self.rpc({"jsonrpc": "2.0", "id": 10, "method": "ping"})
        self.assertEqual(r["id"], 10)

    def test_unknown_method_error(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "no/such"})
        self.assertEqual(r["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
