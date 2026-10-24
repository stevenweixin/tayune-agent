"""Production widget against loopback-only fixtures; no model, database or proxy."""
import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from app.api.answer_stream import sse_event, stream_answer

ROOT = Path(__file__).resolve().parents[1]
Mode = Literal["stream", "json", "retry", "limited", "extended", "expired", "conflict", "malformed", "disabled"]
app = FastAPI()
records = []
executions = set()
attempts = {}
session_count = 0


@app.get("/")
async def index(mode: Mode = "stream"):
    return HTMLResponse("""<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>TAYUNE 1.4.0 local preflight</title>
    <style>body{margin:0;background:#d8e1d5;color:#263b2c;font-family:system-ui;min-height:100vh}
    main{padding:32px;max-width:60ch}input,button{padding:12px}h1{font-family:Georgia}</style>
    <main><h1>Local widget preflight</h1><p>Fixed responses only. No live product or model requests.</p>
    <form role="search"><label>Ordinary search <input name="s"></label><button>Search</button></form></main>
    <script>window.TAYUNE_TEA_COMPANION={apiBase:location.origin+'/fixture/'+""" + json.dumps(mode) + """,
    chatEnabled:""" + ("false" if mode == "disabled" else "true") + """,homePage:false,
    contactUrl:'https://tayunetea.com/policies/shipping-returns/',
    privacyUrl:'https://tayunetea.com/policies/privacy-policy/',shopUrl:'https://tayunetea.com/shop/'};</script>
    <script src="/widget.js"></script></html>""")


@app.get("/widget.js")
async def bundle():
    return FileResponse(ROOT / "deploy/wordpress/assets/tea_companion.js")


@app.get("/__stats")
async def stats():
    return {"records": records, "simulated_executions": len(executions)}


@app.post("/fixture/{mode}/api/session")
async def session(mode: Mode):
    global session_count
    session_count += 1
    records.append({"mode": mode, "path": "session"})
    return {"session_token": "fixture-session-" + str(session_count)}


@app.post("/fixture/{mode}/api/chat")
async def chat(mode: Mode, request: Request):
    body = await request.json()
    records.append({"mode": mode, "path": "chat", "body": body,
                    "fixture_authorization": request.headers.get("authorization")})
    attempts[mode] = attempts.get(mode, 0) + 1
    count = attempts[mode]
    if mode == "limited" and count == 1:
        return JSONResponse({"detail": "fixture limit"}, status_code=429, headers={"Retry-After": "2"})
    if mode == "extended" and count == 1:
        # 截止时间延长但ceil后的数字仍为9；检查倒计时不会因state未变而停掉。
        await asyncio.sleep(0.4)
        return JSONResponse({"detail": "fixture extended limit"}, status_code=429, headers={"Retry-After": "9"})
    if mode == "expired" and count == 1:
        return JSONResponse({"detail": "fixture expired"}, status_code=401)
    if mode == "conflict" and count == 1:
        return JSONResponse({"detail": "fixture stale"}, status_code=409)
    if mode == "malformed":
        return {"degraded": False, "answer_parts": []}
    key = (mode, request.headers.get("authorization"), body.get("request_id"))
    executions.add(key)
    result = {"request_id": body["request_id"], "degraded": False,
        "answer": "Fixture Jasmine:\nUse the supplied 200 ml recipe. Fixed data, not live advice.",
        "answer_parts": [{"text": "Fixture Jasmine", "url": "https://tayunetea.com/shop/"},
                         {"text": ":\nUse the supplied 200 ml recipe. Fixed data, not live advice."}]}
    if mode == "json":
        result["answer_parts"].append({"text": "<img src=x onerror=alert(1)>", "url": "javascript:alert(1)"})
        return result

    async def frames():
        if mode == "retry" and count == 1:
            yield sse_event("meta", {"request_id": body["request_id"]})
            yield sse_event("delta", {"index": 0, "text": "Partial fixture"})
            return
        async for frame in stream_answer(result):
            yield frame
            await asyncio.sleep(0.15)

    return StreamingResponse(frames(), media_type="text/event-stream")
