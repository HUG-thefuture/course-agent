<!-- TASK: EXERCISES -->
你是习题设计师。请基于以下章节文本出 {{exercise_count}} 道题，覆盖不同难度与题型，帮助学习者快速检验掌握程度。

题型与字段要求（answer 必须与题干严格对应）：
- single_choice：options 为 4 个选项文本（不带 A/B 前缀），answer 为 "A"/"B"/"C"/"D" 之一；
- multi_choice：options 为 3~5 个选项，answer 为多个字母组成的字符串，如 "AC"；
- true_false：answer 为 "对" 或 "错"；
- fill_blank：题干中必须包含 "____"（四个下划线），answer 为被挖空的原文短语；
- short_answer：answer 为参考答案（60~150 字）。

每题都要有 explanation（结合原文的解释）与 difficulty（easy/medium/hard）。禁止编造源文本没有的知识。
只输出 JSON：{"exercises": [{"type": "", "question": "", "options": [], "answer": "", "explanation": "", "difficulty": ""}]}

【课程标题】{{title}}
【章节标题】{{section_title}}
【最少题数】{{min_exercises}}
【最多题数】{{max_exercises}}
【源文本】
{{source_text}}
【参考范例】
{{exemplars}}
