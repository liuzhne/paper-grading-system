"""SQLite 连接级完整性设置。"""

from sqlalchemy import event


def enable_sqlite_foreign_keys(engine):
    """为 SQLite 引擎的每个新连接启用外键约束；其它方言保持不变。"""
    if engine.dialect.name != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine
