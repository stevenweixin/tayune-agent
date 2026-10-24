"""事实回答只选择本轮完整证据单元；正文由程序生成，不执行模型自由改写。"""
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.answer_guard import SHORT_REPLIES, check_answer

MAX_UNITS_PER_SOURCE = 80


class EvidenceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(pattern=r"^S[1-9][0-9]*$", max_length=20)
    evidence_id: str = Field(pattern=r"^E[1-9][0-9]*$", max_length=20)


class GroundedReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selections: list[EvidenceSelection] = Field(min_length=1, max_length=6)


def product_facts(product):
    """把已校验的结构化商品字段转成固定事实，不用模型猜测币种、库存或规格。"""
    facts = []
    currency = product.get("currency")
    price = product.get("price")
    price_range = product.get("price_range") or {}
    if currency and price_range.get("min") is not None and price_range.get("max") is not None:
        facts.append(f"Listed price range: {currency} {price_range['min']} to {currency} {price_range['max']}.")
    elif currency and price is not None:
        facts.append(f"Listed price: {currency} {price}.")
    else:
        facts.append("The listed price could not be confirmed.")
    stock = product.get("is_in_stock")
    if stock is True:
        facts.append("Availability: in stock.")
    elif stock is False:
        facts.append("Availability: out of stock.")
    else:
        facts.append("Availability could not be confirmed.")
    if product.get("has_options"):
        facts.append("This product has options; select a specification to confirm its price.")
    facts.append("Listed prices only; final price, shipping and tax are confirmed at checkout.")
    return facts


def record_units(sources, source_id, texts):
    """同一来源分配稳定E编号；保留原行，不接受模型自行截取的引用文字。"""
    if source_id is None:
        return []
    source = next(item for item in sources if item["source_id"] == source_id)
    units = source.setdefault("evidence_units", [])
    current = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for paragraph in text.splitlines():
            paragraph = " ".join(paragraph.split())
            if not paragraph:
                continue
            existing = None
            for unit in units:
                if unit["text"] == paragraph:
                    existing = unit
                    break
            if existing is None:
                if len(units) >= MAX_UNITS_PER_SOURCE:
                    # 超过上限的材料不向模型提供可选编号，不能只截断半条规则再让它引用。
                    continue
                existing = {"evidence_id": "E" + str(len(units) + 1), "text": paragraph}
                units.append(existing)
            if existing not in current:
                current.append(dict(existing))
    return current


def check_grounded_answer(text, sources):
    """只接受证据编号或固定短句；不把任意模型文本拼进用户可见正文。"""
    answer = text.strip()
    if answer in SHORT_REPLIES:
        return check_answer(answer, [])
    if not answer or len(answer) > 8000:
        raise ValueError("invalid_grounded_response")
    try:
        reply = GroundedReply.model_validate_json(answer)
    except (ValidationError, ValueError):
        raise ValueError("invalid_grounded_response") from None
    source_map = {source["source_id"]: source for source in sources}
    selected = {}
    for selection in reply.selections:
        source = source_map.get(selection.source_id)
        if source is None:
            raise ValueError("unknown_source_id")
        units = {unit["evidence_id"]: unit["text"] for unit in source.get("evidence_units", [])}
        if selection.evidence_id not in units:
            raise ValueError("unknown_evidence_id")
        chosen = selected.setdefault(selection.source_id, [])
        value = units[selection.evidence_id]
        if value not in chosen:
            chosen.append(value)
    parts = []
    links = []
    seen_urls = set()
    for source_id, paragraphs in selected.items():
        source = source_map[source_id]
        if parts:
            parts.append({"text": "\n\n"})
        title_part = {"text": source["title"]}
        if source.get("url"):
            title_part["url"] = source["url"]
            if source["url"] not in seen_urls:
                links.append({"title": source["title"], "url": source["url"]})
                seen_urls.add(source["url"])
        else:
            title_part["text"] += " (internal guide)"
        parts.append(title_part)
        # 正文始终取自服务端保存的单元，而不是JSON里的任意text/URL/自定义标题。
        parts.append({"text": ":\n" + "\n".join(paragraphs)})
    rendered = "".join(part["text"] for part in parts)
    if len(rendered) > 12000:
        # 整体拒绝而不是截掉结尾，避免遗漏条件或否定句。
        raise ValueError("grounded_answer_too_long")
    return {"answer": rendered, "answer_parts": parts,
            "sources": links, "evidence_ids": list(selected), "degraded": False,
            "grounding_mode": "extractive_units_v1"}
