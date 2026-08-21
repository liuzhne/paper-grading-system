from sqlalchemy import text

from backend.app.cli.db import make_engine


def test_cli_sqlite_engine_enables_foreign_keys(tmp_path):
    engine = make_engine("sqlite+pysqlite:///%s" % (tmp_path / "integrity.db"))
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    finally:
        engine.dispose()
