import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.core.config import settings
from backend.app.db.sqlite import enable_sqlite_foreign_keys


def _engine_options(database_url: str):
    options = {"pool_pre_ping": True}
    url = make_url(database_url)
    serverless = bool(os.getenv("VERCEL"))
    transaction_pooler = url.get_backend_name() == "postgresql" and url.port == 6543
    if serverless or transaction_pooler:
        options["poolclass"] = NullPool
    if transaction_pooler and url.drivername.endswith("+psycopg"):
        # Supavisor transaction mode does not support prepared statements.
        options["connect_args"] = {"prepare_threshold": None}
    return options


engine = create_engine(settings.DATABASE_URL, **_engine_options(settings.DATABASE_URL))
enable_sqlite_foreign_keys(engine)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
