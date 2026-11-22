"""FastAPI 入口。

- /api/health 免登录（healthcheck 用）
- /api/auth/login 免登录
- 其余 /api/** 默认要求登录
- 静态 UI 由本服务托管，单端口
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import auth as auth_routes
from .api.routes import candidates as candidates_routes
from .api.routes import config as config_routes
from .api.routes import health as health_routes
from .api.routes import jobs as jobs_routes
from .api.routes import libraries as libraries_routes
from .api.routes import providers as providers_routes
from .api.routes import videos as videos_routes
from .auth import init_auth_from_config
from .config import get_config_store
from .worker.runner import WorkerRunner
from .worker.scheduler import ScanScheduler
from .worker.tasks import TaskContext

PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/docs", "/openapi.json"}


_worker: WorkerRunner | None = None
_scheduler: ScanScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker, _scheduler
    store = get_config_store()
    init_auth_from_config(db_path=str(store.db_path))
    ctx = TaskContext(db_path=str(store.db_path), config_store=store)
    _worker = WorkerRunner(ctx)
    _scheduler = ScanScheduler(ctx)
    _worker.start()
    _scheduler.start()
    try:
        yield
    finally:
        if _scheduler:
            _scheduler.stop()
        if _worker:
            _worker.stop()


app = FastAPI(
    title="subangler", version=__version__, docs_url="/docs", redoc_url=None, lifespan=lifespan
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS and not path.startswith("/api/health"):
        from .auth import get_auth_manager

        token = request.cookies.get("subangler_session", "")
        if not get_auth_manager().validate_session(token):
            return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
    return await call_next(request)


app.include_router(health_routes.router)
app.include_router(auth_routes.router)
app.include_router(config_routes.router)
app.include_router(libraries_routes.router)
app.include_router(videos_routes.router)
app.include_router(jobs_routes.router)
app.include_router(candidates_routes.router)
app.include_router(providers_routes.router)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(status_code=404, content={"detail": "not found"})
else:

    @app.get("/", include_in_schema=False)
    def root():
        return {
            "name": "subangler",
            "version": __version__,
            "note": "M0 API 已就绪；Web UI 将在 M3 接入。Swagger 文档见 /docs",
        }


def main() -> None:
    import uvicorn

    host = os.environ.get("SUBANGLER_HOST", "0.0.0.0")
    port = int(os.environ.get("SUBANGLER_PORT", "8765"))
    uvicorn.run("subangler.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
