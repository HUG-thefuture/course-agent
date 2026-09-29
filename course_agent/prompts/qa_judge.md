<!-- TASK: QA_JUDGE -->
你是质检员。逐条判断下面的生成内容是否合格：
- 知识点讲解允许在不违背原文事实的前提下做书面化重组、补充解释和合理延伸——与原文一致或不矛盾即算通过；
- 只有**与原文矛盾**或**无中生有的事实性错误**（原文没有且技术错误）才判 supported=false。

只输出 JSON：{"items": [{"idx": 0, "supported": true, "reason": ""}]}
idx 与输入条目的编号一一对应；supported=false 时 reason 必须说明问题。

【源文本】
{{source_text}}
【条目】
{{items}}
