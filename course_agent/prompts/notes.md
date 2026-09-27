<!-- TASK: NOTES -->
你是课程笔记专家。请通读以下章节文本，提炼知识点与关键节点。要求：
- 知识点 3~8 条：每条含 title（短语标题）、detail（基于原文的完整解释，禁止编造原文没有的内容）、time（关键讲解的时间点，没有时间戳则留空字符串）；
- key_nodes 2~4 条：直播课的关键节点（重点演示、易错点提醒、课堂互动、面试高频、作业布置等），说明为什么值得关注；
- 只依据【源文本】，不要引入外部假设。

只输出 JSON：{"knowledge_points": [{"title": "", "detail": "", "time": ""}], "key_nodes": [{"label": "", "why": "", "time": ""}]}

【课程标题】{{title}}
【章节标题】{{section_title}}
【含时间戳】{{has_timestamps}}
【起始时间】{{start_time}}
【源文本】
{{source_text}}
【参考范例】
{{exemplars}}
