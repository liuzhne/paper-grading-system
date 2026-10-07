"""Enforce one enabled private AI connection per owner and organization.

Revision ID: 0032_single_active_ai_connection
Revises: 0031_rubric_import_sessions
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_single_active_ai_connection"
down_revision = "0031_rubric_import_sessions"
branch_labels = None
depends_on = None


def upgrade():
    # Prefer the most recently verified existing active connection, then oldest
    # creation/id. Never enable a deliberately disabled/deleted connection.
    op.execute(sa.text("""
        UPDATE ai_connections SET status = 'disabled', disabled_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY owner_id, organization_id
                    ORDER BY CASE WHEN last_verified_at IS NULL THEN 1 ELSE 0 END,
                             last_verified_at DESC, created_at ASC, id ASC
                ) AS position
                FROM ai_connections WHERE status = 'active'
            ) AS ranked WHERE position > 1
        )
    """))
    op.create_index(
        "uq_ai_connections_one_active", "ai_connections",
        ["owner_id", "organization_id"], unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade():
    # Retain the user's current statuses; dropping this constraint must not
    # silently reactivate credentials or rewrite task snapshots.
    op.drop_index("uq_ai_connections_one_active", table_name="ai_connections")
