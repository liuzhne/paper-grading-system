"""Add encrypted private BYOK connections and task snapshots.

Revision ID: 0021_private_ai_connections
Revises: 0020_rubric_visibility_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "0021_private_ai_connections"
down_revision = "0020_rubric_visibility_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False, server_default="private"),
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
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("scope = 'private'", name="ck_ai_connections_private_scope"),
        sa.CheckConstraint("provider_type IN ('openai_responses', 'openai_compatible')", name="ck_ai_connections_provider_type"),
        sa.CheckConstraint("status IN ('active', 'disabled', 'deleted')", name="ck_ai_connections_status"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_ai_connections_owner_name"),
    )
    op.create_index("ix_ai_connections_organization_id", "ai_connections", ["organization_id"])
    op.create_index("ix_ai_connections_owner_id", "ai_connections", ["owner_id"])
    op.add_column("grading_batches", sa.Column("ai_connection_id", sa.String(length=36), nullable=True))
    op.add_column("grading_batches", sa.Column("ai_connection_key_version", sa.Integer(), nullable=True))
    op.add_column("grading_batches", sa.Column("ai_connection_snapshot", sa.JSON(), nullable=True))
    op.add_column("evaluation_batches", sa.Column("ai_connection_id", sa.String(length=36), nullable=True))
    op.add_column("evaluation_batches", sa.Column("ai_connection_key_version", sa.Integer(), nullable=True))
    op.add_column("evaluation_batches", sa.Column("ai_connection_snapshot", sa.JSON(), nullable=True))
    op.add_column("scoring_runs", sa.Column("ai_connection_id", sa.String(length=36), nullable=True))
    op.add_column("scoring_runs", sa.Column("ai_connection_key_version", sa.Integer(), nullable=True))
    op.add_column("scoring_runs", sa.Column("ai_connection_snapshot", sa.JSON(), nullable=True))
    op.create_index("ix_scoring_runs_ai_connection_id", "scoring_runs", ["ai_connection_id"])
    op.create_table(
        "ai_usage_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("ai_connection_id", sa.String(length=36), nullable=False),
        sa.Column("scoring_run_id", sa.String(length=36), nullable=True),
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Numeric(12, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["ai_connection_id"], ["ai_connections.id"]),
        sa.ForeignKeyConstraint(["scoring_run_id"], ["scoring_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_usage_ledger_organization_id", "ai_usage_ledger", ["organization_id"])
    op.create_index("ix_ai_usage_ledger_ai_connection_id", "ai_usage_ledger", ["ai_connection_id"])
    op.create_index("ix_ai_usage_ledger_scoring_run_id", "ai_usage_ledger", ["scoring_run_id"])


def downgrade():
    connection = op.get_bind()
    for table in ("ai_usage_ledger", "ai_connections"):
        if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % table)).scalar():
            raise RuntimeError("0021_private_ai_connections downgrade would lose BYOK data")
    for table in ("grading_batches", "evaluation_batches", "scoring_runs"):
        if connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM %s WHERE ai_connection_id IS NOT NULL)" % table)
        ).scalar():
            raise RuntimeError("0021_private_ai_connections downgrade would lose task connection snapshots")
    op.drop_index("ix_ai_usage_ledger_scoring_run_id", table_name="ai_usage_ledger")
    op.drop_index("ix_ai_usage_ledger_ai_connection_id", table_name="ai_usage_ledger")
    op.drop_index("ix_ai_usage_ledger_organization_id", table_name="ai_usage_ledger")
    op.drop_table("ai_usage_ledger")
    op.drop_index("ix_scoring_runs_ai_connection_id", table_name="scoring_runs")
    with op.batch_alter_table("scoring_runs") as batch:
        batch.drop_column("ai_connection_snapshot")
        batch.drop_column("ai_connection_key_version")
        batch.drop_column("ai_connection_id")
    with op.batch_alter_table("grading_batches") as batch:
        batch.drop_column("ai_connection_snapshot")
        batch.drop_column("ai_connection_key_version")
        batch.drop_column("ai_connection_id")
    with op.batch_alter_table("evaluation_batches") as batch:
        batch.drop_column("ai_connection_snapshot")
        batch.drop_column("ai_connection_key_version")
        batch.drop_column("ai_connection_id")
    op.drop_index("ix_ai_connections_owner_id", table_name="ai_connections")
    op.drop_index("ix_ai_connections_organization_id", table_name="ai_connections")
    op.drop_table("ai_connections")
