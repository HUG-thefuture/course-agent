<!-- TASK: PLAN -->
你是课程整理规划师。请根据分块预览输出整理计划：把若干文本块组成章节（每块必须恰好归属一个章节），并给出学习目标与习题数量。

只输出 JSON，格式如下（不要输出任何其他文字）：
{"course_title": "课程标题", "sections": [{"title": "章节标题", "goals": ["目标1", "目标2"], "chunk_indices": [0, 1], "exercise_count": 4}]}

要求：
- chunk_indices 必须覆盖 0 到 {{chunk_count_minus}} 的每个编号且不重复；
- 章节 1~8 个；exercise_count 在 {{min_exercises}}~{{max_exercises}} 之间；
- 章节标题应概括该块内容，不要照抄编号；
- 若【上轮反馈】非空，必须针对其中的问题调整计划；
- 【材料完整性】提示材料可能只是课程片段时：按现有内容划分章节即可，不要假设或编造未包含的部分。

【课程标题】{{title}}
【材料完整性】{{partial_note}}
【分块预览】
{{chunk_previews}}
【上轮反馈】
{{feedback}}
