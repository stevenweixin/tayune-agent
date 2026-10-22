"""
输入：模型最终文本、本轮工具登记的来源。
输出：answer、answer_parts、sources、degraded；不合格时抛出 ValueError，让图决定是否纠正。
这是项目自己的普通 Python 校验，不是 LangChain API，也不是事实正确性评分器。

"""

import re

# 只识别项目约定的来源编号，不把模型自己编写的网址变成可点击链接。
REFERENCE = re.compile(r"\[([^\[\]\n]{1,240})\]\(source:(S[1-9][0-9]*)\)|\[(S[1-9][0-9]*)\]")


FALLBACK = "I couldn't verify enough information right now. Please try again later or use the shop's contact page."

# 无来源时不接受模型自由编写的正文，避免把配方包装成“澄清问题”绕过检查。
# 模型只能选择一个代码，实际展示的英文由程序提供。这是低频首版的有意取舍：
# 更可控，但澄清措辞不如自由生成灵活；不是靠茶名或用户问题关键词打补丁。
SHORT_REPLIES = {
    "<reply:greeting>": "Hello! What would you like to know about our teas, brewing, or shop policies?",
    "<reply:thanks>": "You're welcome! Is there anything else you'd like help with?",
    "<reply:tea_preferences>": "What flavors do you enjoy, and do you have a budget in mind?",
    "<reply:budget>": "What is your budget and currency, and what type of tea are you looking for?",
    "<reply:brewing_setup>": "Which tea are you brewing, and are you using a mug, teapot, or gaiwan?",
    "<reply:which_product>": "Which product do you mean? Please share its name or public shop link.",
    "<reply:policy_topic>": "Which shop policy would you like help with: shipping, returns, or something else?",
    "<reply:unavailable>": FALLBACK,
}

# 这段由 system.py 放入系统提示；只改变最终文本，不改变原生 tool_calls 协议。
ANSWER_RULES = """
FINAL RESPONSE CONTRACT:
For substantive answers about products, brewing, tea facts or policies, first obtain
relevant evidence through the supplied tools IN THIS TURN, even for follow-up questions.
History helps identify 'that tea' but is not current evidence. Cite each supported
factual statement using the source_id supplied by the relevant tool, e.g. [S1].
A product price source does not support a brewing recipe unless the returned content
actually includes that recipe. Do not attach an unrelated citation to pass validation.
Do not add recipes, numbers, or policy terms that are absent from the tool evidence.
An approved internal guide can have source_id with url=null. It is valid evidence,
not a public webpage. Cite its source_id normally; the server adds a plain-text
reference title instead of a clickable link. Never invent a URL for that guide.
An internal guide for another tea is not evidence of the requested tea's own recipe.
When naming a product or public document, make its title an inline source link:
[Short product or document title](source:S1). Use only the current tool's source_id.
Do not write actual URLs. The server supplies the registered URL. Ordinary [S1]
citations remain valid; do not add a separate Sources list at the bottom.

When no factual answer is appropriate, output ONLY one exact code below, no other text:
<reply:greeting> : greeting only
<reply:thanks> : acknowledgment only
<reply:tea_preferences> : essential taste/budget preferences are missing
<reply:budget> : essential budget/currency information is missing
<reply:brewing_setup> : the tea or brewing vessel cannot be identified from context
<reply:which_product> : the product cannot be identified from context
<reply:policy_topic> : the policy topic cannot be identified from context
<reply:unavailable> : tools failed, or evidence cannot support the requested answer
The server expands these codes into fixed English replies. Do not write your own
uncited greeting, clarification or failure message. Never invent missing details.
If context already identifies the tea or vessel, retrieve evidence instead of asking
the user to repeat it. Normal tool calls remain available before the final response.
"""

REPAIR_PROMPT = """Internal answer validation rejected the previous draft.
Return to the latest real user question. Obtain relevant tool evidence if needed,
then answer with current source IDs such as [S1]. Do not reuse old-turn citations.
If clarification is essential or evidence is unavailable, use exactly one permitted
<reply:...> code from the system contract. No uncited factual draft may be released.
This correction shares the existing model/tool budgets; it does not reset them.
"""

def check_answer(text: str, candidates: list[dict]) -> dict:
    """只检查可程序化的契约，不判断自然语言是否被来源逐句支持。"""
    answer = text.strip()
    if not answer:
        raise ValueError("empty_answer")

    if answer in SHORT_REPLIES:
        return {
            "answer": SHORT_REPLIES[answer],
            "answer_parts": [{"text": SHORT_REPLIES[answer]}],
            "sources": [],
            "degraded": answer == "<reply:unavailable>",
        }
    if "<reply:" in answer:
        # 必须完整匹配代码；“代码 + 未核验配方”不能作为安全短句放行。
        raise ValueError("invalid_reply_code")

    # 正则只识别固定格式的引用编号，不尝试用正则理解冲泡知识。
    references = list(REFERENCE.finditer(answer))
    if not references:
        raise ValueError("missing_citations")
    remaining = REFERENCE.sub("", answer)
    if "](source:" in remaining:
        raise ValueError("invalid_inline_link")

    sources_by_id = {}
    for source in candidates:
        sources_by_id[source["source_id"]] = source

    selected = []
    seen_ids = set()
    internal_titles = []
    for reference in references:
        # 前两个捕获组是“标题链接”的文字和ID；第三个是旧格式[S1]的ID。
        source_id = reference.group(2) or reference.group(3)
        if source_id not in sources_by_id:
            raise ValueError("unknown_source_id")
        if source_id not in seen_ids:
            source = sources_by_id[source_id]
            if source["url"]:
                selected.append({"title": source["title"], "url": source["url"]})
            elif source["title"] not in internal_titles:
                # 内部指南可以被引用，但不能把文件路径/片段编号伪装成网页链接。
                # 只展示已审核的文档标题，sources字段仍只含真实可点击网页。
                internal_titles.append(source["title"])
            seen_ids.add(source_id)

    plain_answer = REFERENCE.sub(lambda match: match.group(1) or "", answer).strip()
    if not plain_answer:
        raise ValueError("empty_answer")

    explicit_links = set()
    for reference in references:
        if reference.group(2):
            explicit_links.add(reference.group(2))
    parts = []
    linked_ids = set()
    cursor = 0
    for reference in references:
        if reference.start() > cursor:
            parts.append({"text": answer[cursor:reference.start()]})
        source_id = reference.group(2) or reference.group(3)
        source = sources_by_id[source_id]
        if reference.group(1):
            part = {"text": reference.group(1)}
            if source["url"]:
                part["url"] = source["url"]
                linked_ids.add(source_id)
            parts.append(part)
        elif source["url"] and source_id not in explicit_links and source_id not in linked_ids:
            # 兼容旧[S1]回答：把引用标题放在该句旁边，而不是集中到底部。
            parts.extend([{"text": " ("}, {"text": source["title"], "url": source["url"]}, {"text": ")"}])
            linked_ids.add(source_id)
        cursor = reference.end()
    if cursor < len(answer):
        parts.append({"text": answer[cursor:]})
    if internal_titles:
        parts.append({"text": "\n\nShop reference (internal guide; no public link): " + "; ".join(internal_titles)})
    answer = "".join(part["text"] for part in parts).strip()
    return {"answer": answer, "answer_parts": parts, "sources": selected, "degraded": False}
