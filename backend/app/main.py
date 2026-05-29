from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import ProgrammingError

from backend.app.api.routes import batches
from backend.app.api.routes import exports
from backend.app.api.routes import papers
from backend.app.api.routes import rubrics
from backend.app.api.routes import scoring
from backend.app.api.routes import system
from backend.app.core.config import settings
from backend.app.services.storage.local import ensure_storage_dirs


@asynccontextmanager
async def lifespan(app):
    ensure_storage_dirs()
    yield


def create_app():
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

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

    app.include_router(batches.router, prefix=settings.API_PREFIX)
    app.include_router(rubrics.router, prefix=settings.API_PREFIX)
    app.include_router(papers.router, prefix=settings.API_PREFIX)
    app.include_router(scoring.router, prefix=settings.API_PREFIX)
    app.include_router(exports.router, prefix=settings.API_PREFIX)
    app.include_router(system.router, prefix=settings.API_PREFIX)

    web_dir = Path(__file__).resolve().parents[2] / "frontend" / "web"
    assets_dir = web_dir / "assets"
    if web_dir.exists() and assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/", include_in_schema=False)
        def web_app():
            return FileResponse(web_dir / "index.html")

    return app


app = create_app()
