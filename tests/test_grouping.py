"""增量自动归组（course_agent.grouping.place_new_courses）回归。

用假嵌入器（按块文本关键词返回固定二维向量）避免测试依赖 Ollama。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def make_course(root: Path, rel: str, title: str, blocks: list[str],
                passed: bool = True) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps(
        {"title": title, "passed": passed, "qa_score": 90},
        ensure_ascii=False), encoding="utf-8")
    (d / "sections.json").write_text(json.dumps([{
        "title": "第一节",
        "knowledge_points": [{"title": b, "concept": "概念", "time": ""}
                             for b in blocks]}], ensure_ascii=False),
        encoding="utf-8")
    return d


class FakeEmbedder:
    """块文本含 vue → [1,0]，否则 [0,1]；记录调用次数供缓存断言。"""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise OSError("Ollama 不可用")
        return [[1.0, 0.0] if "vue" in t.lower() else [0.0, 1.0] for t in texts]


class TestPlaceNewCourses(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "output"
        self.root.mkdir(parents=True)
        self.cfg = SimpleNamespace(output_dir=str(self.root),
                                   data_dir=str(Path(self._td.name) / "data"))
        # 既有组：成员是 vue 课
        self.member = make_course(self.root, "vue-base", "Vue3 基础入门", ["vue 组件"])
        gdir = self.root / "课程整合" / "Vue3-前端框架"
        gdir.mkdir(parents=True)
        (gdir / "组.json").write_text(json.dumps(
            {"name": "Vue3 前端框架", "courses": ["vue-base"]},
            ensure_ascii=False), encoding="utf-8")
        (gdir / "整合复习.md").write_text("# Vue3 前端框架 · 整合复习\n旧版", encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _place(self, items, fail=False):
        from course_agent import grouping
        fake = FakeEmbedder(fail=fail)
        import course_agent.kb_vector as kbv
        orig = kbv.OllamaEmbedder
        kbv.OllamaEmbedder = lambda *a, **k: fake
        try:
            info = grouping.place_new_courses(items, self.cfg, llm=None,
                                              threshold=0.68)
        finally:
            kbv.OllamaEmbedder = orig
        return info, fake

    def test_content_similarity_joins_existing_group(self):
        new = make_course(self.root, "webui/v-new", "Vue 路由实战", ["vue-router 使用"])
        info, _ = self._place([("webui/v-new", new)])
        a = info["assigned"][0]
        self.assertEqual(a["group"], "Vue3 前端框架")
        self.assertEqual(a["method"], "content")
        gj = json.loads((self.root / "课程整合" / "Vue3-前端框架" / "组.json")
                        .read_text(encoding="utf-8"))
        self.assertIn("webui/v-new", gj["courses"])

    def test_dissimilar_starts_new_group_by_seed(self):
        # pandas 内容与 vue 组不相似 → 按课号/关键词种子新建组（50 号在 pandas 系列）
        new = make_course(self.root, "webui/pd", "50、pandas分组聚合", ["pandas groupby"])
        info, _ = self._place([("webui/pd", new)])
        a = info["assigned"][0]
        self.assertEqual(a["group"], "数据分析与可视化（Pandas 栈）")
        gj = json.loads((self.root / "课程整合" / "数据分析与可视化（Pandas-栈）" / "组.json")
                        .read_text(encoding="utf-8"))
        self.assertIn("webui/pd", gj["courses"])

    def test_embed_failure_falls_back_to_seed(self):
        new = make_course(self.root, "webui/pd", "50、pandas分组聚合", ["pandas groupby"])
        info, fake = self._place([("webui/pd", new)], fail=True)
        a = info["assigned"][0]
        self.assertIn(a["method"], ("seed", "new"))  # 未走 content 相似度
        self.assertIsNotNone(info["embed_error"])
        # 嵌入失败也必须落组（可稍后 --force 修正）
        gj = json.loads((self.root / "课程整合" / "数据分析与可视化（Pandas-栈）" / "组.json")
                        .read_text(encoding="utf-8"))
        self.assertIn("webui/pd", gj["courses"])

    def test_vectors_cached_across_runs(self):
        c1 = make_course(self.root, "webui/v1", "Vue 组件", ["vue 组件"])
        c2 = make_course(self.root, "webui/v2", "Vue 状态", ["vue 状态"])
        self._place([("webui/v1", c1)])
        # 第二次运行：vue-base/v1 向量已落盘缓存，只需嵌入 v2 自己
        _, fake2 = self._place([("webui/v2", c2)])
        self.assertEqual(fake2.calls, 1)

    def test_backfill_finds_ungrouped_passed_only(self):
        from course_agent import grouping
        make_course(self.root, "webui/in1", "课A", ["a"], passed=True)
        make_course(self.root, "webui/bad", "课B", ["b"], passed=False)
        missing = grouping.missing_passed_courses(self.cfg)
        ids = [cid for cid, _ in missing]
        self.assertIn("webui/in1", ids)
        self.assertNotIn("webui/bad", ids)      # 未过质检不归组
        self.assertNotIn("vue-base", ids)       # 已在组里不重复归

    def test_llm_integration_written_when_available(self):
        class OkLLM:
            def chat(self, system, user, **kw):
                return "# 标题\n整合内容" * 100
        new = make_course(self.root, "webui/v-new", "Vue 路由实战", ["vue-router 使用"])
        from course_agent import grouping
        import course_agent.kb_vector as kbv
        orig = kbv.OllamaEmbedder
        kbv.OllamaEmbedder = lambda *a, **k: FakeEmbedder()
        try:
            grouping.place_new_courses([("webui/v-new", new)], self.cfg, llm=OkLLM())
        finally:
            kbv.OllamaEmbedder = orig
        md = (self.root / "课程整合" / "Vue3-前端框架" / "整合复习.md")
        self.assertIn("整合内容", md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
