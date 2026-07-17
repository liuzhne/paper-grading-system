from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.db.sqlite import enable_sqlite_foreign_keys

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
enable_sqlite_foreign_keys(engine)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
