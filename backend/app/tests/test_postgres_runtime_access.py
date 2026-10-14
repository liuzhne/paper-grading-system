"""Runtime DML checks, with an opt-in regression against CI's initialized PostgreSQL."""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend.app.services.deployment.postgres_verifier import (
    RUNTIME_ACCESS_TABLES,
    _verify_runtime_table_access,
    verify_postgres,
)


PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}


@pytest.fixture
def catalog():
    """Execute the verifier SQL, emulating PostgreSQL's ANY-privilege semantics."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    granted = set(PRIVILEGES)
    with engine.connect() as connection:
        def has_table_privilege(role, table_oid, privileges):
            assert role == "pgs_app"
            assert table_oid == 42  # The public table, regardless of search_path.
            return bool(granted.intersection(p.strip() for p in privileges.split(",")))

        connection.connection.driver_connection.create_function(
            "has_table_privilege", 3, has_table_privilege
        )
        connection.exec_driver_sql("CREATE TABLE pg_namespace (oid INTEGER, nspname TEXT)")
        connection.exec_driver_sql(
            "CREATE TABLE pg_class (oid INTEGER, relnamespace INTEGER, "
            "relname TEXT, relrowsecurity BOOLEAN)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE pg_policies (schemaname TEXT, tablename TEXT, policyname TEXT)"
        )
        connection.exec_driver_sql("INSERT INTO pg_namespace VALUES (1, 'public')")
        connection.exec_driver_sql(
            "INSERT INTO pg_class VALUES (42, 1, 'rule_scoring_tasks', true)"
        )
        connection.exec_driver_sql(
            "INSERT INTO pg_policies VALUES ('public', 'rule_scoring_tasks', 'pgs_app_dml')"
        )
        yield connection, granted
    engine.dispose()


@pytest.mark.parametrize(
    "granted",
    [PRIVILEGES - {privilege} for privilege in sorted(PRIVILEGES)] + [{"SELECT"}, set()],
    ids=["missing-delete", "missing-insert", "missing-select", "missing-update", "select-only", "none"],
)
def test_runtime_check_rejects_any_missing_dml_privilege(catalog, granted):
    connection, actual = catalog
    actual.clear()
    actual.update(granted)
    with pytest.raises(RuntimeError, match="runtime role access/RLS is incomplete for rule_scoring_tasks"):
        _verify_runtime_table_access(connection, "rule_scoring_tasks")


def test_runtime_check_accepts_all_dml_with_rls_and_policy(catalog):
    connection, _ = catalog
    _verify_runtime_table_access(connection, "rule_scoring_tasks")


@pytest.mark.parametrize("change", [
    "UPDATE pg_class SET relrowsecurity = false",
    "DELETE FROM pg_policies",
    "DELETE FROM pg_class",
])
def test_runtime_check_still_requires_table_rls_and_policy(catalog, change):
    connection, _ = catalog
    connection.exec_driver_sql(change)
    with pytest.raises(RuntimeError, match="runtime role access/RLS is incomplete"):
        _verify_runtime_table_access(connection, "rule_scoring_tasks")


def test_postgres_verifier_rejects_each_revoked_runtime_privilege():
    """CI runs this after init_database, using its local disposable owner connection.

    Each REVOKE is rolled back before the next check. Never use a remote database.
    """
    url = os.environ.get("PGS_TEST_POSTGRES_VERIFIER_URL", "").strip()
    if not url:
        pytest.skip("PGS_TEST_POSTGRES_VERIFIER_URL is not set")
    parsed = make_url(url)
    if (parsed.get_backend_name() != "postgresql"
            or parsed.host not in {"localhost", "127.0.0.1", "::1"}
            or parsed.database != "pgs_init_check"):
        pytest.fail("PostgreSQL verifier tests require the local disposable pgs_init_check database")

    engine = create_engine(url)
    try:
        with Session(engine) as session:
            assert session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')"))
            verify_postgres(session)
            for table in RUNTIME_ACCESS_TABLES:
                for privilege in sorted(PRIVILEGES):
                    savepoint = session.begin_nested()
                    try:
                        # Both identifiers are fixed by the test/code, never user input.
                        session.execute(text('REVOKE %s ON TABLE public."%s" FROM pgs_app' % (privilege, table)))
                        with pytest.raises(RuntimeError, match="runtime role access/RLS is incomplete for %s$" % table):
                            verify_postgres(session)
                    finally:
                        savepoint.rollback()
            verify_postgres(session)
            session.rollback()
    finally:
        engine.dispose()
