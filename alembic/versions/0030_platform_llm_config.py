"""Persist the platform default model as an admin-configured singleton.

Revision ID: 0030_platform_llm_config
Revises: 0029_runtime_access_for_v2_tables
Create Date: 2026-09-09

D-028.  The platform model used to come entirely from environment variables, so
there was no record of who configured it, when, or to what.  It now lives in a
single row an administrator writes from the operations page, and a fresh
deployment starts with **no** platform model at all — which is what makes
"scoring without a bound connection" impossible by construction rather than
dependent on some env happening to be set correctly.

The key reuses the BYOK envelope encryption but under a **different AAD domain**
(`platform-llm|…` rather than `ai-connection|…`), so neither side's ciphertext
can be moved into the other's row and still decrypt.

Not reusing ``ai_connections``: its ``organization_id`` and ``owner_id`` are NOT
NULL and it carries ``CheckConstraint("scope = 'private'")``.  Weakening a
multi-tenant table's integrity constraints to host one singleton row costs more
than a separate table.
"""

from alembic import op
import sqlalchemy as sa


revision = "0030_platform_llm_config"
down_revision = "0029_runtime_access_for_v2_tables"
branch_labels = None
depends_on = None


TABLE = "platform_llm_config"


def _configure_postgres_runtime_access():
    """Match the production least-privilege role when that role exists.

    The runtime role has no DDL and does not inherit rights on new tables.
    Local and CI databases never create ``pgs_app``, so a missing grant is
    invisible to both — it surfaces only as ``permission denied`` in production
    *after* a migration that reported success.
    """

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
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("provider_options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("api_key_nonce", sa.String(length=128), nullable=False),
        sa.Column("api_key_tag", sa.String(length=128), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("key_last4", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("configured_by", sa.String(length=36), nullable=False),
        sa.Column("configured_at", sa.DateTime(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_platform_llm_config_status",
        ),
    )
    _configure_postgres_runtime_access()


def downgrade() -> None:
    """有配置时拒绝降级。

    这一行里有密钥材料和「谁配的」记录。删掉之后即便重建表，配置也要管理员重新
    录入一次 API key——降级把一次运维动作变成一次需要人找回密钥的事故。空表照常
    允许回滚。
    """
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT count(*) FROM %s" % TABLE)
    ).scalar_one()
    if count:
        raise RuntimeError(
            "0030 downgrade would drop the platform LLM configuration "
            "(%d row(s)), including the encrypted key and who configured it. "
            "Export it and disable the config explicitly before downgrading."
            % count
        )
    op.drop_table(TABLE)
