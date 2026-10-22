"""FastAPI 应用入口。

只监听127.0.0.1:8011，单进程。请从项目根目录python -m app.main。
"""

import os
import secrets
from ipaddress import ip_address
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from app.core.runtime import Runtime, ROOT
from app.agent.service import TeaService
from app.api.schemas.chat import ChatInput
from app.core.logger import setup_logging
from app.api.request_limits import BodyLimitMiddleware
from app.api.rate_limit import RequestRateLimiter,get_visitor_ip

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

    api = FastAPI(title="TAYUNE Tea Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
    allowed_origins = ["https://tayunetea.com", "https://www.tayunetea.com"]
    # 每个应用实例一个计数器；不为每次请求新建，否则计数会失效。
    api.state.request_limiter = RequestRateLimiter()
    api.add_middleware(BodyLimitMiddleware)

    @api.middleware("http")
    async def basic_limits(request: Request, call_next):
        # 隧道层也必须限制正文大小；这里防止普通浏览器提交超大JSON。
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 8192):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Request too large"}, status_code=413,
                                headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        if request.url.path in ("/api/session", "/api/chat"):
            # 返回值含会话凭据或回答，浏览器和代理不应复用缓存。
            response.headers["Cache-Control"] = "no-store"
        return response

    def check_origin(request):
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins and origin != "http://127.0.0.1:8011":
            raise HTTPException(403, "Origin not allowed")
        # CORS/Origin不是鉴权，curl可伪造。低流量首版靠全站日限额兜住费用；
        # 本轮新增IP限频，不代表完成身份认证，也不保证日额度不被他人消耗。

    def check_request_rate(request):
        peer_host = request.client.host if request.client else "unknown"
        visitor_ip = get_visitor_ip(peer_host, request.headers.get("cf-connecting-ip"))
        wait_seconds = api.state.request_limiter.check(visitor_ip, request.url.path)
        if wait_seconds:
            raise HTTPException(429, "Too many requests. Please wait and try again.",
                                headers={"Retry-After": str(wait_seconds)})

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
        check_request_rate(request)
        try:
            token = request.app.state.runtime.sessions.create()
        except RuntimeError:
            raise HTTPException(429,"Too many sessions") from None
        return {"session_token":token}

    @api.post("/api/chat")
    async def chat(body:ChatInput,request:Request,authorization:str = Header(default="")):
        check_origin(request)
        check_request_rate(request)
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

    def check_local_widget(request: Request):
        # 不把本地启用聊天的测试页暴露到隧道；不扩大CORS或公网路径白名单。
        peer = request.client.host if request.client else ""
        try:
            local_peer = ip_address(peer).is_loopback
        except ValueError:
            local_peer = False
        local_host = request.url.hostname in ("127.0.0.1", "localhost", "::1")
        if not local_peer or not local_host or "cf-connecting-ip" in request.headers:
            raise HTTPException(404, "Not found")

    @api.get("/demo/widget")
    async def widget_demo(request: Request):
        check_local_widget(request)
        return FileResponse(ROOT / "web/widget_local.html", headers={"Cache-Control": "no-store"})

    @api.get("/demo/assets/tea_companion.js")
    async def widget_asset(request: Request):
        check_local_widget(request)
        return FileResponse(ROOT / "deploy/wordpress/assets/tea_companion.js",
                            media_type="application/javascript", headers={"Cache-Control": "no-store"})
    # 最后添加的中间件位于外层，才能给BodyLimit和basic_limits的413也加上CORS。
    # 不删除旧中间件的限制，只调整CORS位置；OPTIONS由它处理，不消耗接口额度。
    api.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"],
                       expose_headers=["Retry-After"])

    return api

app = create_app()

if __name__ == "__main__":
    import uvicorn
    # 本模块自己处理可信CF访客地址，不让通用代理头先改写request.client。
    uvicorn.run(app, host="127.0.0.1", port=8011, workers=1, access_log=False,
                proxy_headers=False)
