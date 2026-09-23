"""Add database-backed temporary rubric import sessions.

Revision ID: 0031_rubric_import_sessions
Revises: 0030_platform_llm_config
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "0031_rubric_import_sessions"
down_revision = "0030_platform_llm_config"
branch_labels = None
depends_on = None


TABLE = "rubric_import_sessions"


def _configure_postgres_runtime_access() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
    ).scalar()
    if not role_exists:
        return
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % TABLE)
    op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % TABLE)
    op.execute("DROP POLICY IF EXISTS pgs_app_dml ON %s" % TABLE)
    op.execute(
        "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
        "USING (true) WITH CHECK (true)" % TABLE
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("prepared_graph", sa.JSON(), nullable=False),
        sa.Column("draft_data", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("score_adjustments", sa.JSON(), nullable=False),
        sa.Column("rules_file_name", sa.Text(), nullable=True),
        sa.Column("rules_file_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("template_file_name", sa.Text(), nullable=True),
        sa.Column("template_file_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("confirmation_key", sa.String(length=200), nullable=True),
        sa.Column("rubric_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'expired', 'cancelled')",
            name="ck_rubric_import_sessions_status",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_rubric_import_sessions_positive_version",
        ),
        sa.CheckConstraint(
            "total_score >= 0",
            name="ck_rubric_import_sessions_nonnegative_total",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["rubric_id"], ["rubrics.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "confirmation_key",
            name="uq_rubric_import_sessions_owner_confirmation_key",
        ),
    )
    op.create_index(
        "ix_rubric_import_sessions_organization_id",
        TABLE,
        ["organization_id"],
    )
    op.create_index("ix_rubric_import_sessions_owner_id", TABLE, ["owner_id"])
    op.create_index("ix_rubric_import_sessions_rubric_id", TABLE, ["rubric_id"])
    _configure_postgres_runtime_access()


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(sa.text("SELECT count(*) FROM %s" % TABLE)).scalar_one()
    if count:
        raise RuntimeError(
            "0031 downgrade would drop %d rubric import session(s); "
            "expire or cancel and archive them before downgrading" % count
        )
    op.drop_index("ix_rubric_import_sessions_rubric_id", table_name=TABLE)
    op.drop_index("ix_rubric_import_sessions_owner_id", table_name=TABLE)
    op.drop_index("ix_rubric_import_sessions_organization_id", table_name=TABLE)
    op.drop_table(TABLE)
