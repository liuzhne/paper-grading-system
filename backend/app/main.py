from contextlib import asynccontextmanager
import json
from pathlib import Path
from time import perf_counter

from fastapi import Depends
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import ProgrammingError

from backend.app.api.deps import enforce_auth
from backend.app.api.routes import auth
from backend.app.api.routes import ai_connections
from backend.app.api.routes import batches
from backend.app.api.routes import batch_jobs
from backend.app.api.routes import calibration
from backend.app.api.routes import exports
from backend.app.api.routes import organizations
from backend.app.api.routes import papers
from backend.app.api.routes import release_gates
from backend.app.api.routes import rubrics
from backend.app.api.routes import scoring
from backend.app.api.routes import system
from backend.app.api.routes import submissions_v2
from backend.app.core.config import settings
from backend.app.services.observability import begin_request_timing
from backend.app.services.observability import end_request_timing
from backend.app.services.observability import server_timing_header
from backend.app.services.storage.local import ensure_storage_dirs


@asynccontextmanager
async def lifespan(app):
    ensure_storage_dirs()
    yield


def _inject_client_config(html: str) -> str:
    """Inject the served API prefix into a browser entrypoint.

    The path comes from the running service configuration; the frontend offers
    no editable entry for it. JSON encoding keeps special characters in the
    configuration from breaking out of the page's script context. Both the
    legacy SPA and the v2 workbench read the same ``window.__PGS_CONFIG__``.
    """

    client_config = json.dumps(
        {"apiBase": settings.API_PREFIX.rstrip("/")}, ensure_ascii=False
    ).replace("</", "<\\/")
    return html.replace(
        "<!-- PGS_CLIENT_CONFIG -->",
        f"<script>window.__PGS_CONFIG__ = {client_config};</script>",
        1,
    )


def create_app():
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

    @app.middleware("http")
    async def attach_server_timing(request, call_next):
        started_at = perf_counter()
        token = begin_request_timing()
        try:
            response = await call_next(request)
            response.headers["Server-Timing"] = server_timing_header(
                (perf_counter() - started_at) * 1000
            )
            return response
        finally:
            end_request_timing(token)

    @app.exception_handler(OperationalError)
    async def database_operational_error_handler(request, exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "数据库暂不可用，请确认 PostgreSQL 已启动，或使用本地 SQLite DATABASE_URL 重新启动后端。"
                )
            },
        )

    @app.exception_handler(ProgrammingError)
    async def database_programming_error_handler(request, exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "数据库表尚未初始化或结构不匹配，请运行 uv run alembic upgrade head 后重试。"
                )
            },
        )

    # 数据类路由经 enforce_auth 门禁（opt-in：AUTH_ENABLED=False 时为 no-op）；
    # auth / system 保持开放（登录页与状态自检需在登录前可达）。
    guarded = [Depends(enforce_auth)]
    app.include_router(auth.router, prefix=settings.API_PREFIX)
    app.include_router(ai_connections.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(organizations.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(batches.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(batch_jobs.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(rubrics.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(papers.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(
        release_gates.router,
        prefix=settings.API_PREFIX,
        dependencies=guarded,
    )
    app.include_router(scoring.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(
        submissions_v2.router,
        prefix=settings.API_PREFIX,
        dependencies=guarded,
    )
    app.include_router(exports.router, prefix=settings.API_PREFIX, dependencies=guarded)
    app.include_router(system.router, prefix=settings.API_PREFIX)
    app.include_router(calibration.router, prefix=settings.API_PREFIX, dependencies=guarded)

    repo_root = Path(__file__).resolve().parents[2]

    # v2 评审工作台（计划 §8.3）：新页统一挂在 /workbench/ 下，与旧 SPA 并存。
    # 托管的是 scripts/build_web_static.py 的统一组装产物，而非 Vite 源码目录。
    # 资源 mount 必须先于 SPA 回退注册：缺失的 JS 要 404，不能回 HTML。
    workbench_dir = repo_root / "public" / "workbench"
    workbench_assets = workbench_dir / "assets"
    if (workbench_dir / "index.html").is_file() and workbench_assets.is_dir():
        app.mount(
            "/workbench/assets",
            StaticFiles(directory=workbench_assets),
            name="workbench-assets",
        )

        @app.get("/workbench", include_in_schema=False)
        @app.get("/workbench/{path:path}", include_in_schema=False)
        def workbench_app(path: str = ""):
            return HTMLResponse(
                _inject_client_config(
                    (workbench_dir / "index.html").read_text(encoding="utf-8")
                )
            )

    web_dir = repo_root / "frontend" / "web"
    assets_dir = web_dir / "assets"
    if web_dir.exists() and assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/login", include_in_schema=False)
        @app.get("/register", include_in_schema=False)
        @app.get("/reset-password", include_in_schema=False)
        def web_app():
            return HTMLResponse(
                _inject_client_config(
                    (web_dir / "index.html").read_text(encoding="utf-8")
                )
            )

    return app


app = create_app()
