"""在线代码测试运行器 + 判断题收集修复的回归。"""

import unittest

from course_agent.webui import run_code_tests


class TestRunCodeTests(unittest.TestCase):
    def test_python_pass(self):
        code = "def add(a, b):\n    return a + b\n"
        r = run_code_tests(code, ["assert add(1, 2) == 3",
                                  "assert add(-1, 1) == 0"], "python")
        self.assertTrue(r["runnable"])
        self.assertEqual(r["passed"], 2)
        self.assertEqual(r["total"], 2)

    def test_python_fail_with_output(self):
        code = "def add(a, b):\n    return a - b\n"
        r = run_code_tests(code, ["assert add(1, 2) == 3"], "python")
        self.assertEqual(r["passed"], 0)
        self.assertIn("AssertionError", r["results"][0]["output"])

    def test_each_test_isolated(self):
        code = "def f(x):\n    return x * 2\n"
        r = run_code_tests(code, ["assert f(2) == 4", "assert f(0) == 1"], "python")
        # 第二条失败但第一条不受影响
        self.assertTrue(r["results"][0]["passed"])
        self.assertFalse(r["results"][1]["passed"])

    def test_timeout_killed(self):
        code = "import time\nwhile True:\n    time.sleep(0.05)\n"
        r = run_code_tests(code, ["assert False"], "python", timeout=2)
        self.assertFalse(r["results"][0]["passed"])
        self.assertIn("超时", r["results"][0]["output"])

    def test_unsupported_language(self):
        r = run_code_tests("<div></div>", [], "other")
        self.assertFalse(r["runnable"])
        self.assertIn("不支持", r["reason"])

    def test_javascript_requires_node(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("本机无 node")
        r = run_code_tests("function add(a,b){return a+b}", ["assert(add(1,2)===3)"],
                           "javascript")
        self.assertTrue(r["runnable"])
        self.assertEqual(r["passed"], 1)


if __name__ == "__main__":
    unittest.main()
