<!-- TASK: CODING -->
你是编程实战题设计师。请基于以下章节内容，出 1~2 道动手编程题，让学习者把课程里讲的功能亲手实现一遍。

字段要求：
- type 固定为 "coding_practice"；
- question：明确的任务描述，贴合课程场景（如某个组件、函数、配置、命令操作），说清楚要实现什么、输入输出是什么、有什么约束。**函数/算法类题目必须在题干中明确规定函数名与参数**，与 tests 保持一致；
- starter_code：可选。给学习者一个可起步的代码骨架（用 TODO 注释标出待实现处），语言/框架与课程一致；
- answer：完整的参考实现代码（可直接运行或粘贴即用）；
- hints：2~3 条思路提示，只引导方向，不直接给答案；
- check_points：2~4 条验收要点，学习者可自行对照检查；
- explanation：实现要点解析，说明为什么这样写、容易错在哪；
- difficulty：easy / medium / hard；
- language："python" / "javascript" / "other"（按课程技术栈判断）；
- tests：**在线测试断言**（这是判分依据，非常重要）：
  - 当题目是纯函数/算法类（可断言验证）：给出 2~4 条测试，每条一行可独立执行的断言——python 用 `assert 表达式`，javascript 用 `assert(条件)`（配合题干规定函数名）；
  - 断言必须覆盖正常输入与边界情况，且与你给出的参考实现语义完全一致；
  - 框架组件/页面/配置类题目无法断言验证时，tests 留空数组 []，学习者交卷后由 AI 对照验收要点评审。

重要约束：
- 题目必须源自【源文本】中讲到或演示过的功能，禁止超出课程范围的想象；
- 若【源文本】完全不涉及代码、命令或配置操作，只输出 {"exercises": []}；
- 只输出 JSON：{"exercises": [{"type": "coding_practice", "question": "", "starter_code": "", "answer": "", "hints": [], "check_points": [], "explanation": "", "difficulty": "", "language": "", "tests": []}]}

【课程标题】{{title}}
【章节标题】{{section_title}}
【源文本】
{{source_text}}
