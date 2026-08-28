import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.core.config import settings
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.services.observability import install_sqlalchemy_timing
from backend.app.services.observability import note_session_opened


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
install_sqlalchemy_timing(engine)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def get_db():
    db = SessionLocal()
    note_session_opened()
    try:
        yield db
    finally:
        db.close()
