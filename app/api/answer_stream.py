"""核验完成的答案分段交付；不是未经审核的模型Token流。"""
import asyncio
import json


def sse_event(event: str, data: dict) -> str:
    # json.dumps会转义正文里的换行，避免把一段答案误拆成多个SSE事件。
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_answer(result: dict):
    """这里只接收service.chat完成后的结果，不执行工具、不重试、不再次调用模型。"""
    yield sse_event("meta", {"request_id": result["request_id"]})
    parts = result.get("answer_parts") or [{"text": result["answer"]}]
    for index, part in enumerate(parts):
        text = part["text"]
        for start in range(0, len(text), 12):
            chunk = {"index": index, "text": text[start:start + 12]}
            if part.get("url"):
                chunk["url"] = part["url"]
            yield sse_event("delta", chunk)
            # 让浏览器逐段显示；等待期间不占服务推理锁，也不会阻塞其他协程。
            await asyncio.sleep(0.02)
    yield sse_event("done", {"request_id": result["request_id"], "degraded": result["degraded"]})
