import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from workmemory.agent.runtime import AgentManager
from workmemory.api.agent import router as agent_router
from workmemory.api.capabilities import router as capabilities_router
from workmemory.api.coding import router as coding_router
from workmemory.api.companion import router as companion_router
from workmemory.api.model_settings import router as models_router
from workmemory.api.pets import PetUploadLimit
from workmemory.api.pets import router as pets_router
from workmemory.api.proactive import router as proactive_router
from workmemory.api.reminders import router as reminders_router
from workmemory.api.routes import router
from workmemory.companion.proactive import loop as proactive_loop
from workmemory.config import ROOT, get_settings
from workmemory.config.generation import GenerationStore
from workmemory.db import make_engine, make_sessions
from workmemory.inference.generation import ModelConfigurationChanged
from workmemory.inference.ollama import Ollama
from workmemory.jobs.runtime_lock import acquire_runtime_lock
from workmemory.jobs.worker import maintenance_loop, worker
from workmemory.memory.sync import MemoryRegistry, initialize
from workmemory.pets.skins import MAX_UPLOAD_BYTES, SkinLibrary
from workmemory.reminders.scheduler import reminder_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# External libraries may include prompt text in debug/info logs. Keep them out of persisted logs.
for name in ["httpx", "httpcore", "graphiti_core", "neo4j"]:
    logging.getLogger(name).setLevel(logging.CRITICAL)


def create_app(
    settings=None, *, engine=None, ollama=None, memory=None, run_worker=True, generation_store=None, mcp_store=None, pet_library=None
):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        db = engine or make_engine(settings.database_url)
        sessions = make_sessions(db)
        store = generation_store or GenerationStore(settings.ollama)
        model = ollama or Ollama(settings.ollama, store.current)
        registry = memory or MemoryRegistry(settings, model)
        leader = None
        local_lock = None
        task = None
        maintenance_task = None
        reminder_task = None
        proactive_task = None
        local_maintenance = None
        agent = None
        try:
            # One API process owns the in-process model scheduler and embedded ReMe workspace.
            if run_worker:
                local_lock = acquire_runtime_lock(ROOT / ".run/runtime.lock")
                leader = await db.connect()
                if not await leader.scalar(text("SELECT pg_try_advisory_lock(738452901)")):
                    raise RuntimeError(
                        "Another WorkMemory runtime is active; use a single API worker"
                    )
                await leader.commit()
            async with sessions.begin() as session:
                await initialize(session, settings.memory.backend)
            await model.start()
            rt = SimpleNamespace(
                settings=settings,
                engine=db,
                sessions=sessions,
                ollama=model,
                memory=registry,
                generation_store=store,
                pet_library=pet_library or SkinLibrary(),
            )
            from workmemory.mcp.runtime import MCPManager
            rt.mcp = MCPManager(rt, mcp_store)
            agent = AgentManager(rt)
            rt.agent = agent
            app.state.runtime = rt
            from workmemory.evidence.archive import initialize_policy
            await initialize_policy(rt)
            if run_worker:
                await agent.recover()
                task = asyncio.create_task(worker(rt))
                maintenance_task = asyncio.create_task(maintenance_loop(rt))
                reminder_task = asyncio.create_task(reminder_loop(rt))
                proactive_task = asyncio.create_task(proactive_loop(rt))
                from workmemory.maintenance.service import loop as local_loop
                local_maintenance = asyncio.create_task(local_loop(rt))
            yield
        finally:
            if local_maintenance:
                local_maintenance.cancel()
                await asyncio.gather(local_maintenance, return_exceptions=True)
            if proactive_task:
                proactive_task.cancel()
                await asyncio.gather(proactive_task, return_exceptions=True)
            if agent:
                await agent.close()
            if reminder_task:
                reminder_task.cancel()
                await asyncio.gather(reminder_task, return_exceptions=True)
            if maintenance_task:
                maintenance_task.cancel()
                await asyncio.gather(maintenance_task, return_exceptions=True)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(registry.close(), model.close(), return_exceptions=True)
            try:
                if leader:
                    try:
                        await leader.execute(text("SELECT pg_advisory_unlock(738452901)"))
                    except SQLAlchemyError:
                        # A restarted database already released its session locks.
                        pass
                    finally:
                        await leader.close()
                await db.dispose()
            finally:
                if local_lock:
                    local_lock.close()

    app = FastAPI(
        title="工作记忆助手", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    app.add_middleware(PetUploadLimit)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path.startswith("/api"):
            if request.url.path.rstrip("/") == "/api/mcp":
                return await call_next(request)
            authorization = request.headers.get("authorization", "")
            if not secrets.compare_digest(authorization, f"Bearer {settings.api_token}"):
                return JSONResponse({"detail": "请使用本机访问令牌登录"}, status_code=401)
            origin = request.headers.get("origin")
            if origin and origin not in {
                f"http://127.0.0.1:{settings.server.port}",
                f"http://localhost:{settings.server.port}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            }:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
            size = request.headers.get("content-length")
            if size:
                try:
                    length = int(size)
                    if length < 0:
                        raise ValueError
                except ValueError:
                    return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
                if length > (MAX_UPLOAD_BYTES if request.url.path == "/api/pet/skins" and request.method == "POST" else 4 * 1024 * 1024):
                    return JSONResponse({"detail": "上传文件过大"}, status_code=413)
        response = await call_next(request)
        if request.method in {"POST", "PATCH", "DELETE"} and response.status_code < 300 and request.url.path.startswith("/api/") and not any(
            name in request.url.path for name in ("/ingest", "/collector", "/client/", "/test", "/probe", "/evidence/resolve", "/mcp")
        ):
            from workmemory.companion.state import emit
            async with request.app.state.runtime.sessions.begin() as session:
                await emit(session)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(
            {"detail": "数据库暂不可用，采集端会保留本地待上传记录。"}, status_code=503
        )

    @app.exception_handler(httpx.HTTPError)
    async def inference_error(request, exc):
        return JSONResponse(
            {"detail": "模型服务暂不可用，请在偏好与状态中检查生成服务和本地嵌入模型。"},
            status_code=503,
        )

    @app.exception_handler(ModelConfigurationChanged)
    async def model_changed(request, exc):
        return JSONResponse({"detail": "生成模型配置已更新，请重试本次请求。"}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default error response can echo an entire body containing an API key.
        return JSONResponse(
            {"detail": "请求格式无效，请检查填写的字段和参数范围。"}, status_code=422
        )

    @app.exception_handler(ValueError)
    async def invalid_model_response(request, exc):
        return JSONResponse(
            {"detail": "无法验证模型结果，请检查模型输出设置或重试。"}, status_code=502
        )

    @app.get("/health")
    async def health():
        async with app.state.runtime.sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "postgresql"}

    from workmemory.api.optimization import router as optimization_router
    app.include_router(optimization_router)
    app.include_router(router)
    app.include_router(reminders_router)
    app.include_router(models_router)
    app.include_router(agent_router)
    app.include_router(coding_router)
    app.include_router(companion_router)
    app.include_router(proactive_router)
    app.include_router(pets_router)
    app.include_router(capabilities_router)
    class MCPApplication:
        async def __call__(self, scope, receive, send):
            await app.state.runtime.mcp.asgi(scope, receive, send)

    mcp_app = MCPApplication()
    # Register the exact endpoint before the static catch-all, which would otherwise
    # swallow /api/mcp before Starlette can redirect to the mounted trailing slash.
    app.add_route("/api/mcp", mcp_app, methods=["GET", "POST", "DELETE"], name="mcp_endpoint")
    app.mount("/api/mcp", mcp_app, name="mcp")
    if (ROOT / "frontend/dist").exists():
        app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True), name="frontend")
    return app
