"""把已归档的实际工具可见证据投影为评测输入，不补检索、不改历史回答。"""
import hashlib
import json
from pathlib import Path


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tool_contexts(messages):
    contexts, metadata = [], []
    for message in messages:
        payload = json.loads(message["content"])
        if not payload.get("ok"):
            continue
        name = message["name"]
        data = payload.get("data") or {}
        if name == "search_knowledge":
            # 两种键都存在时只能取一次，不能把同一工具结果复制两遍。
            items = payload.get("passages", data.get("passages", []))
        elif name == "search_products":
            items = data.get("products", [])
        elif name == "get_product":
            items = [data]
        else:
            continue
        for item in items:
            title = item.get("title") or item.get("name")
            units = item.get("evidence_units", [])
            if not title or not units:
                raise ValueError("Archived context lacks actual model-visible evidence units")
            text = title + "\n" + "\n".join(unit["text"] for unit in units)
            # 保留跨工具顺序、重叠片段及重复产品，不能为提高precision偷偷去重。
            contexts.append(text)
            metadata.append({"tool": name, "title": title, "url": item.get("url"),
                             "chunk_id": item.get("chunk_id"), "context_hash": hashlib.sha256(text.encode()).hexdigest()})
    return contexts, metadata


def prepare(archive, reference_file):
    rows = [json.loads(line) for line in Path(archive).read_text(encoding="utf-8").splitlines() if line.strip()]
    labels = json.loads(Path(reference_file).read_text(encoding="utf-8"))["cases"]
    prior_ids = {"multi-oolong-56": "multi-oolong-55", "multi-jasmine-58": "multi-jasmine-57",
                 "multi-policy-60": "multi-policy-59"}
    previous = {(row["profile"], row["id"]): row for row in rows}
    result = []
    for row in rows:
        if row["id"] not in labels:
            continue
        if row["http_status"] != 200:
            raise ValueError("Selected archived response was not HTTP200; record it separately")
        contexts, metadata = tool_contexts(row["tool_messages"])
        user_input = row["question"]
        if row["id"] in prior_ids:
            prior = previous[row["profile"], prior_ids[row["id"]]]
            user_input = ("Previous user question: " + prior["question"] + "\nPrevious assistant response: "
                          + prior["result"]["answer"] + "\nCurrent user question: " + row["question"])
        result.append({"sample_id": row["profile"] + ":" + row["id"], "case_id": row["id"],
            "profile": row["profile"], "category": row["category"], "original_question": row["question"],
            "user_input": user_input, "response": row["result"]["answer"], "retrieved_contexts": contexts,
            "context_metadata": metadata, "reference": labels[row["id"]]["reference"],
            "reference_basis": labels[row["id"]]["basis"], "reference_titles": labels[row["id"]]["source_titles"],
            "degraded": row["result"].get("degraded"), "original_task_id": row["result"].get("task_id"),
            "input_projection": "single_turn_with_prior_text" if row["id"] in prior_ids else "single_turn"})
    expected = {(profile, key) for profile in ("sequential", "concurrent4") for key in labels}
    actual = {(row["profile"], row["case_id"]) for row in result}
    if actual != expected or len(result) != len(actual):
        raise ValueError("Archive/reference pairs are incomplete or duplicated")
    return result
