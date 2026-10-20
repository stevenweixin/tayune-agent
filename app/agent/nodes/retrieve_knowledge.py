"""为当前问题检索可靠知识。

TODO：
1. 根据意图决定检索分类和过滤条件。
2. 调用 retriever 获取候选片段。
3. 保留 score、source_url 和 source_id。
4. 没有可靠结果时设置 fallback 原因。
"""

