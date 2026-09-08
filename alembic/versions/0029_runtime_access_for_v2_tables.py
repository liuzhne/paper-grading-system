"""Grant the production runtime role access to the tables 0026/0027 created.

Revision ID: 0029_runtime_access_for_v2_tables
Revises: 0028_export_event_backfill
Create Date: 2026-09-08

Production runs as ``pgs_app``, a least-privilege role with **no DDL** that does
not automatically gain rights on new tables.  0023 put that grant inside the
migration itself ("new tables must not depend on an out-of-band grant/policy
step"); 0026 and 0027 created tables without doing the same.

The failure mode is not a failed migration.  It is a migration that **succeeds
and leaves the application broken**: the schema reaches head, then the runtime
role hits ``permission denied`` the moment it touches
``review_command_receipts`` or ``export_events``.  That is harder to recognise
than the original outage, because the reported revision looks correct.

Local and CI databases never create ``pgs_app``, so this gap is invisible to
both.  Everything here is conditional on the role existing.

Re-entrant on purpose: the two tables already exist in environments that ran
0026/0027, and a plain ``CREATE POLICY`` would fail on a second pass.
"""

from alembic import op
import sqlalchemy as sa


revision = "0029_runtime_access_for_v2_tables"
down_revision = "0028_export_event_backfill"
branch_labels = None
depends_on = None


TABLES = ("review_command_receipts", "export_events")


def _pgs_app_exists(connection) -> bool:
    if connection.dialect.name != "postgresql":
        return False
    return bool(
        connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
        ).scalar()
    )


def upgrade() -> None:
    connection = op.get_bind()
    if not _pgs_app_exists(connection):
        return
    for table in TABLES:
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % table
        )
        op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % table)
        # 幂等：这两张表在跑过 0026/0027 的环境里已经存在，重复 CREATE POLICY
        # 会直接失败。删了重建比 IF NOT EXISTS 更明确——策略定义以本迁移为准。
        op.execute("DROP POLICY IF EXISTS pgs_app_dml ON %s" % table)
        op.execute(
            "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
            "USING (true) WITH CHECK (true)" % table
        )


def downgrade() -> None:
    """撤销授权。

    不做「有数据就拒绝」的判断：这里没有数据，只有权限。撤掉它不会丢任何东西，
    而拒绝降级反而会把一次权限回滚变成必须手工处理的事。
    """
    connection = op.get_bind()
    if not _pgs_app_exists(connection):
        return
    for table in TABLES:
        op.execute("DROP POLICY IF EXISTS pgs_app_dml ON %s" % table)
        op.execute("ALTER TABLE %s DISABLE ROW LEVEL SECURITY" % table)
        op.execute(
            "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE %s FROM pgs_app" % table
        )
