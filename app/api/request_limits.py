"""ASGI入口按实际字节限制正文，包括没有Content-Length的分块请求。

在请求正文被 FastAPI 读完之前,
先自己一块一块接收并累计字节数,超过 8KB 或 10 秒没数据就直接拒绝,
合格的再"重播"给后面的应用。

先接收最多8KB，再交给FastAPI；ASGI是框架和HTTP服务器之间的接口。
"""
import asyncio
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes=8192):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            try:
                event = await asyncio.wait_for(receive(), timeout=10)
            except TimeoutError:
                return await JSONResponse({"detail": "Request timed out"}, status_code=408)(scope, receive, send)
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > self.max_bytes:
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
            if not event.get("more_body", False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)