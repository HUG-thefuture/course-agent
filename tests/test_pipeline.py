"""管线：习题校验、计划校验、结构校验、完整编排循环。"""

import tempfile
import unittest
from pathlib import Path

from course_agent.config import AgentConfig, PipelineConfig
from course_agent.ingest.loader import load_source
from course_agent.llm import MockLLM
from course_agent.pipeline.executor import ExecuteError, generate_section
from course_agent.pipeline.models import (Chunk, Exercise, KnowledgePoint,
                                          Plan, PlanSection, SectionOutput)
from course_agent.pipeline.planner import PlanError, make_plan, validate_plan_structure
from course_agent.pipeline.tester import (structural_check, validate_exercise,
                                          run_qa)
from course_agent.pipeline.orchestrator import Orchestrator
from course_agent.outputs import write_all

EXAMPLE_TXT = Path(__file__).resolve().parent.parent / "examples" / "sample_lecture.txt"


def _valid_exercise(**over):
    d = {
        "type": "single_choice",
        "question": "下列说法正确的是？",
        "options": ["正确的说法", "错误一", "错误二", "错误三"],
        "answer": "A",
        "explanation": "因为原文提到……",
        "difficulty": "easy",
    }
    d.update(over)
    return d


class TestValidateExercise(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_exercise(_valid_exercise()), [])

    def test_missing_answer(self):
        self.assertTrue(validate_exercise(_valid_exercise(answer="")))

    def test_fill_blank_requires_blank(self):
        errs = validate_exercise(_valid_exercise(type="fill_blank",
                                                 question="没有空格", answer="x",
                                                 options=[]))
        self.assertTrue(any("____" in e for e in errs))

    def test_fill_blank_ok(self):
        errs = validate_exercise(_valid_exercise(type="fill_blank",
                                                 question="____是什么", answer="事务",
                                                 options=[]))
        self.assertEqual(errs, [])

    def test_true_false(self):
        self.assertEqual(validate_exercise(_valid_exercise(
            type="true_false", answer="对", options=[])), [])
        self.assertTrue(validate_exercise(_valid_exercise(
            type="true_false", answer="也许", options=[])))

    def test_multi_choice(self):
        self.assertEqual(validate_exercise(_valid_exercise(
            type="multi_choice", answer="AC", options=["一", "二", "三"])), [])
        self.assertTrue(validate_exercise(_valid_exercise(
            type="multi_choice", answer="AZ", options=["一", "二", "三"])))

    def test_single_choice_multiple_letters(self):
        self.assertTrue(validate_exercise(_valid_exercise(answer="AB")))

    def test_answer_out_of_options(self):
        self.assertTrue(validate_exercise(_valid_exercise(
            answer="E", options=["一", "二"])))


def _plan_ok():
    return Plan(course_title="课", sections=[
        PlanSection(title="第一节", goals=["g"], chunk_indices=[0], exercise_count=3)])


class TestPlanValidation(unittest.TestCase):
    def test_missing_coverage(self):
        chunks = [Chunk(i, "t") for i in range(3)]
        plan = Plan(course_title="课", sections=[
            PlanSection(title="A", chunk_indices=[0, 1])])
        issues = validate_plan_structure(plan, chunks, PipelineConfig())
        self.assertTrue(any("未" in i and "覆盖" in i for i in issues))

    def test_duplicate_chunk(self):
        chunks = [Chunk(i, "t") for i in range(2)]
        plan = Plan(course_title="课", sections=[
            PlanSection(title="A", chunk_indices=[0, 0, 1])])
        issues = validate_plan_structure(plan, chunks, PipelineConfig())
        self.assertTrue(any("重复" in i for i in issues))

    def test_out_of_range(self):
        chunks = [Chunk(0, "t")]
        plan = Plan(course_title="课", sections=[
            PlanSection(title="A", chunk_indices=[0, 5])])
        issues = validate_plan_structure(plan, chunks, PipelineConfig())
        self.assertTrue(any("不存在" in i for i in issues))

    def test_ok(self):
        chunks = [Chunk(i, "t") for i in range(3)]
        plan = Plan(course_title="课", sections=[
            PlanSection(title="A", chunk_indices=[0, 1]),
            PlanSection(title="B", chunk_indices=[2])])
        self.assertEqual(validate_plan_structure(plan, chunks, PipelineConfig()), [])

    def test_make_plan_with_mock(self):
        llm = MockLLM()
        chunks = [Chunk(i, f"内容{i}。讲一些知识点，涉及事务与并发。") for i in range(2)]
        plan = make_plan(llm, "测试课", chunks, "", PipelineConfig())
        self.assertEqual(len(plan.sections), 2)
        self.assertEqual(sum(len(s.chunk_indices) for s in plan.sections), 2)

    def test_make_plan_gives_up(self):
        llm = MockLLM(fail_plan=99)
        chunks = [Chunk(0, "内容。")]
        with self.assertRaises(PlanError):
            make_plan(llm, "测试课", chunks, "", PipelineConfig())


def _section_ok(title="第一节"):
    return SectionOutput(
        title=title,
        knowledge_points=[KnowledgePoint(title="要点", detail="事务基于 AOP 代理实现")],
        exercises=[Exercise(**_valid_exercise()),
                   Exercise(**_valid_exercise(question="第二题", type="true_false",
                                              answer="对", options=[]))],
        source_excerpt="事务基于 AOP 代理实现",
    )


class TestStructuralCheck(unittest.TestCase):
    def test_ok(self):
        plan = _plan_ok()
        issues = structural_check(plan, [_section_ok()], PipelineConfig())
        self.assertEqual(issues, [])

    def test_missing_kp(self):
        sec = _section_ok()
        sec.knowledge_points = []
        issues = structural_check(_plan_ok(), [sec], PipelineConfig())
        self.assertTrue(any("知识点" in i for i in issues))

    def test_invalid_exercise_detected(self):
        sec = _section_ok()
        sec.exercises[0].answer = "Z"
        issues = structural_check(_plan_ok(), [sec], PipelineConfig())
        self.assertTrue(any("选项编号" in i for i in issues))

    def test_duplicate_question(self):
        a, b = _section_ok(), _section_ok("第二节")
        plan = Plan(course_title="课", sections=[
            PlanSection(title="第一节", chunk_indices=[0]),
            PlanSection(title="第二节", chunk_indices=[1])])
        issues = structural_check(plan, [a, b], PipelineConfig())
        self.assertTrue(any("重复题目" in i for i in issues))


class TestOrchestrator(unittest.TestCase):
    def _run(self, mock, max_attempts=3):
        cfg = AgentConfig()
        cfg.pipeline.max_attempts = max_attempts
        material = load_source(str(EXAMPLE_TXT))
        orch = Orchestrator(mock, cfg)
        return orch.run(material, title="测试直播课")

    def test_happy_path(self):
        result = self._run(MockLLM())
        self.assertTrue(result.passed)
        self.assertEqual(result.qa.score, 100)
        self.assertGreaterEqual(len(result.sections), 1)
        self.assertGreaterEqual(len(result.sections[0].exercises), 2)
        self.assertTrue(all(s.knowledge_points for s in result.sections))

    def test_review_failure_loops_back(self):
        mock = MockLLM(fail_review=1)
        result = self._run(mock)
        self.assertTrue(result.passed)
        self.assertEqual(len(result.attempts), 2)
        self.assertFalse(result.attempts[0].review_passed)

    def test_qa_failure_loops_back_then_gives_up(self):
        mock = MockLLM(fail_qa=99)
        result = self._run(mock, max_attempts=2)
        self.assertFalse(result.passed)
        self.assertEqual(len(result.attempts), 2)
        self.assertFalse(result.attempts[0].qa_passed)
        # 失败也应保留最后一轮产物，便于人工检查
        self.assertTrue(result.sections)

    def test_qa_fail_once_then_pass(self):
        result = self._run(MockLLM(fail_qa=1))
        self.assertTrue(result.passed)
        self.assertEqual(len(result.attempts), 2)

    def test_write_outputs(self):
        result = self._run(MockLLM())
        with tempfile.TemporaryDirectory() as td:
            files = write_all(result, td)
            for name in ("notes.md", "key_nodes.md", "exercises.md",
                         "review_report.md", "plan.json", "sections.json",
                         "session.json"):
                self.assertIn(name, files)
                self.assertTrue(Path(files[name]).is_file())
                self.assertGreater(Path(files[name]).stat().st_size, 0)
            ex = Path(files["exercises.md"]).read_text(encoding="utf-8")
            self.assertIn("参考答案", ex)
            notes = Path(files["notes.md"]).read_text(encoding="utf-8")
            self.assertIn("知识点", notes)


class TestExecutor(unittest.TestCase):
    def test_section_generation_with_mock(self):
        from course_agent.utils import fmt_time
        llm = MockLLM()
        chunks = [Chunk(0, "事务失效场景讲解。自调用导致事务失效，因为绕过了代理对象。" * 3,
                        start_time=30.0)]
        psec = PlanSection(title="事务失效", chunk_indices=[0], exercise_count=4)
        sec = generate_section(llm, "测试课", psec, chunks, PipelineConfig())
        self.assertGreaterEqual(len(sec.knowledge_points), 1)
        self.assertEqual(len(sec.exercises), 4)
        self.assertTrue(any(e.type == "fill_blank" for e in sec.exercises))
        self.assertTrue(sec.source_excerpt)

    def test_out_of_range_chunk(self):
        psec = PlanSection(title="X", chunk_indices=[7])
        with self.assertRaises(ExecuteError):
            generate_section(MockLLM(), "课", psec, [Chunk(0, "t")], PipelineConfig())


if __name__ == "__main__":
    unittest.main()
