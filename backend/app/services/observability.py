"""Low-overhead per-request timings for diagnosing serverless database latency."""

from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.engine import Engine


@dataclass
class DatabaseTiming:
    session_opened_at: float | None = None
    checkout_ms: float | None = None
    sql_started_at: float | None = None
    sql_ms: float = 0.0
    query_count: int = 0


_database_timing: ContextVar[DatabaseTiming | None] = ContextVar(
    "database_timing", default=None
)


def begin_request_timing():
    return _database_timing.set(DatabaseTiming())


def end_request_timing(token) -> None:
    _database_timing.reset(token)


def note_session_opened() -> None:
    timing = _database_timing.get()
    if timing is not None and timing.session_opened_at is None:
        timing.session_opened_at = perf_counter()


def server_timing_header(app_ms: float) -> str:
    timing = _database_timing.get()
    values = [f"app;dur={app_ms:.1f}"]
    if timing is not None and timing.checkout_ms is not None:
        values.append(f"db-checkout;dur={timing.checkout_ms:.1f}")
    if timing is not None and timing.query_count:
        values.append(f"db-sql;dur={timing.sql_ms:.1f};desc=\"{timing.query_count} queries\"")
    return ", ".join(values)


def install_sqlalchemy_timing(engine: Engine) -> None:
    """Attach once to the application engine; timing is active only in a request context."""

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_connection, _connection_record, _connection_proxy):
        timing = _database_timing.get()
        if (
            timing is not None
            and timing.session_opened_at is not None
            and timing.checkout_ms is None
        ):
            timing.checkout_ms = (perf_counter() - timing.session_opened_at) * 1000

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        _connection, _cursor, _statement, _parameters, _context, _executemany
    ):
        timing = _database_timing.get()
        if timing is not None:
            timing.sql_started_at = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        _connection, _cursor, _statement, _parameters, _context, _executemany
    ):
        timing = _database_timing.get()
        if timing is not None and timing.sql_started_at is not None:
            timing.sql_ms += (perf_counter() - timing.sql_started_at) * 1000
            timing.query_count += 1
            timing.sql_started_at = None
