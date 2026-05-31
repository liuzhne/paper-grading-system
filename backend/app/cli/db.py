"""CLI 本地持久化：自建 sqlite 引擎/会话 + 程序化建表（不碰绑 Postgres 的 db.session）。

关键：`alembic/env.py` 在加载时用 `settings.DATABASE_URL` 覆盖 `sqlalchemy.url`，
所以建表前必须先 `configure()` 把本地 sqlite 注入 settings；`ensure_schema` 用 ini-less
`Config()`（`config_file_name=None` → env.py 跳过 fileConfig，避免禁用 app logger）。
"""

import logging
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.services.storage.local import ensure_storage_dirs

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HOME = Path.home() / ".paper-grading"


def sqlite_url(db_path):
    return "sqlite+pysqlite:///%s" % Path(db_path).expanduser().resolve()


def configure(db_path, storage_path):
    """把 CLI 的本地 sqlite + storage 注入全局 settings 并建好目录；返回 sqlite URL。"""
    target = Path(db_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    settings.DATABASE_URL = sqlite_url(target)
    settings.STORAGE_ROOT = Path(storage_path).expanduser()
    ensure_storage_dirs()
    return settings.DATABASE_URL


def ensure_schema():
    """alembic upgrade head（幂等）。env.py 用 settings.DATABASE_URL，故须先 configure()。"""
    logging.getLogger("alembic").setLevel(logging.WARNING)  # 保持 CLI 输出干净
    cfg = Config()  # ini-less：config_file_name=None → env.py 跳过 fileConfig
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")


def make_engine(db_url=None):
    """自建引擎；sqlite 开 WAL + 60s busy_timeout，让 `score --workers` 并发访问不直接报 locked。"""
    url = db_url or settings.DATABASE_URL
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"timeout": 60} if is_sqlite else {},
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


@contextmanager
def cli_session(db_url=None):
    """对本地 sqlite 开一个会话（自建引擎，用完即弃）。"""
    engine = make_engine(db_url)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
