"""本地浏览器固定样例：仅监听回环地址，不加载模型或连接数据库。"""
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from app.agent.answer_guard import check_answer
from app.api.answer_stream import stream_answer


ROOT = Path(__file__).resolve().parents[1]
app = FastAPI()


@app.get("/")
async def index():
    return HTMLResponse("""<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Local SSE fixture</title><body style="background:#e9e5da;padding:40px">
    <h1>LOCAL FIXTURE - NO MODEL CALLS</h1><p>Fixed data, not live product information.</p>
    <script>window.TAYUNE_TEA_COMPANION = {apiBase:location.origin,chatEnabled:true,
    homePage:false,contactUrl:'https://tayunetea.com/policies/shipping-returns/',
    privacyUrl:'https://tayunetea.com/policies/privacy-policy/',shopUrl:'https://tayunetea.com/shop/'};</script>
    <script src="/widget.js"></script></body></html>""")


@app.get("/widget.js")
async def bundle():
    return FileResponse(ROOT / "deploy/wordpress/assets/tea_companion.js")


@app.post("/api/session")
async def session():
    return {"session_token": "local-fixture-only"}


@app.post("/api/chat")
async def chat():
    result = check_answer(
        "Two fixed examples:\n\n- [Example Oolong](source:S1): a sample product title.\n"
        "- [Example Green Tea](source:S2): another sample title.\n\n"
        "These are fixed browser fixtures, not real recommendations or verified shop stock.",
        [{"source_id": "S1", "title": "Example Oolong", "url": "https://tayunetea.com/shop/"},
         {"source_id": "S2", "title": "Example Green Tea", "url": "https://tayunetea.com/shop/"}],
    )
    result["request_id"] = "local-fixture"

    async def slower_fixture():
        # 只在固定样例中放慢交付，方便观察中间状态；正式SSE函数不改速度。
        async for frame in stream_answer(result):
            yield frame
            await asyncio.sleep(0.15)

    return StreamingResponse(slower_fixture(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8715, access_log=False)
