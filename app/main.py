"""FastAPI 应用入口。

参考入口只监听127.0.0.1:8011，单进程。请从项目根目录python -m app.main_1。
"""

import os
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from app.core.runtime import Runtime, ROOT
from app.agent.service import TeaService
from app.api.schemas.chat import ChatInput
from app.core.logger import setup_logging
from app.api.request_limits import BodyLimitMiddleware

def create_app(runtime_factory = Runtime):
    @asynccontextmanager
    async def lifespan(app):
        runtime = runtime_factory()
        try:
            await runtime.start()
            app.state.runtime = runtime
            app.state.service = TeaService(runtime,int(os.getenv("TAYUNE_DAILY_REQUEST_LIMIT", "20")))
            yield
        finally:
            await runtime.close()

    api = FastAPI(title="TAYUNE Tea Assistant", lifespan=lifespan,docs_url=None, redoc_url=None)
    allowed_origins = ["https://tayunetea.com","https://www.tayunetea.com"]
    api.add_middleware(CORSMiddleware,allow_origins=allowed_origins,
                       allow_methods = ["GET","POST"], allow_headers = ["Content-Type", "Authorization"])
    api.add_middleware(BodyLimitMiddleware)

    @api.middleware("http")
    async def basic_limits(request: Request, call_next):
        # 隧道层也必须限制正文大小；这里防止普通浏览器提交超大JSON。
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length)> 8192):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail":"Request too large"}, status_code=413)
        return await call_next(request)

    def check_origin(request):
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins and origin != "http://127.0.0.1:8011":
            raise HTTPException(403,"origin not allowed")
        # CORS/Origin不是鉴权，curl可伪造。低流量首版靠全站日限额兜住费用；
        # 公开上线前在隧道启用速率限制/挑战，不能把任何后台密钥放浏览器。

    @api.get("/health/live")
    async def live():
        return {"status": "alive"}

    @api.get("/health/ready")
    async def ready(request:Request):
        runtime = request.app.state.runtime
        try:
            from app.rag.hybrid_vector_store import COLLECTION
            exists = await runtime.cpu.run(runtime.milvus.has_collection,COLLECTION)
            if not exists:
                raise RuntimeError("collection missing")
        except Exception:
            raise HTTPException(503,"Not ready") from None
        return {"status": "ready"}

    @api.post("/api/session")
    async def session(request:Request):
        check_origin(request)
        try:
            token = request.app.state.runtime.sessions.create()
        except RuntimeError:
            raise HTTPException(429,"Too many sessions") from None
        return {"session_token":token}

    @api.post("/api/chat")
    async def chat(body:ChatInput,request:Request,authorization:str = Header(default="")):
        check_origin(request)
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Session required")
        token = authorization[7:]
        if len(token) > 100 or not body.message.strip():
            raise HTTPException(422,"Invalid input")
        request_id = secrets.token_hex(8)
        try:
            result = await request.app.state.service.chat(token,body.message.strip())
        except LookupError:
            raise HTTPException(401, "Session expired") from None
        except RuntimeError:
            raise HTTPException(429, "Assistant is busy or its request limit was reached") from None
        # 日志只记摘要指标；不记录问题、摘要、session_token和SDK原异常。
        logger = setup_logging(ROOT / "logs/agent.log")
        logger.info("request=%s degraded=%s tools=%d usage=%s", request_id,result["degraded"],
                    len(result.get("trace",[])),result.get("usage"))
        # 工具轨迹只留后端/测试使用，不向用户展示工程细节。
        result.pop("trace", None)
        result["request_id"] = request_id
        return result

    @api.get("/demo")
    async def demo():
        return FileResponse(ROOT/ "web/chat.html")

    return api

app = create_app()


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app,host="127.0.0.1", port=8011, workers=1,access_log=False)