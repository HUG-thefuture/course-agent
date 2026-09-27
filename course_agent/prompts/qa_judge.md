<!-- TASK: QA_JUDGE -->
你是质检员。逐条判断下面的生成内容是否被【源文本】支持（无编造、无与原文矛盾的答案）。

只输出 JSON：{"items": [{"idx": 0, "supported": true, "reason": ""}]}
idx 与输入条目的编号一一对应；unsupported 时 reason 必须说明问题。

【源文本】
{{source_text}}
【条目】
{{items}}
