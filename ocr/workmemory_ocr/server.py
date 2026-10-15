"""Local PNG-to-text endpoint. No image files, request bodies, or OCR text are logged."""

import asyncio
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from workmemory_ocr.engine import Engine, InvalidImage, read_config, versions


def create_app(config=None, engine_factory=Engine):
    config = config or read_config()
    lock = asyncio.Lock()
    metrics = {"requests": 0, "last_seconds": None}

    @asynccontextmanager
    async def lifespan(app):
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="paddleocr") as executor:
            app.state.executor = executor
            app.state.engine = await asyncio.get_running_loop().run_in_executor(
                executor, engine_factory, config
            )
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/health":
            if not secrets.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + config["token"]
            ):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            if request.headers.get("origin"):
                return JSONResponse(
                    {"detail": "Browser image uploads are disabled"}, status_code=403
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    async def health():
        return {
            "ready": True,
            "engine": "paddleocr",
            "versions": versions(),
            "detection_model": config["detection_model"],
            "recognition_model": config["recognition_model"],
            "device": "cpu",
            "cpu_threads": config["cpu_threads"],
            "busy": lock.locked(),
            **metrics,
        }

    @app.post("/ocr")
    async def recognize(request: Request):
        if request.headers.get("content-type") != "image/png":
            return JSONResponse({"detail": "Expected image/png"}, status_code=415)
        if lock.locked():
            return JSONResponse({"detail": "OCR is busy; retry next capture"}, status_code=429)
        async with lock:
            # Stream with a hard cap, including chunked requests without Content-Length.
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > config["max_image_bytes"]:
                    return JSONResponse({"detail": "Image too large"}, status_code=413)
                data.extend(chunk)
            started = time.monotonic()
            future = asyncio.get_running_loop().run_in_executor(
                app.state.executor, app.state.engine.recognize, data
            )
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                # Paddle inference cannot be cancelled. Keep the lease until it really stops.
                try:
                    await future
                finally:
                    raise
            except InvalidImage:
                return JSONResponse(
                    {"detail": "Invalid PNG or excessive dimensions"}, status_code=422
                )
            except Exception:
                return JSONResponse({"detail": "Local OCR failed"}, status_code=503)
            metrics["requests"] += 1
            metrics["last_seconds"] = round(time.monotonic() - started, 3)
            return {**result, "seconds": metrics["last_seconds"]}

    return app


def main():
    import uvicorn

    config = read_config()
    uvicorn.run(
        create_app(config),
        host="127.0.0.1",
        port=config["port"],
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
