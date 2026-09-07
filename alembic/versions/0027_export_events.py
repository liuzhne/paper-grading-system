"""Add event-level export records alongside the legacy spreadsheet log.

Revision ID: 0027_export_events
Revises: 0026_review_command_receipts
Create Date: 2026-09-08

Frontend v2 plan §5-F / §7. Expand only — the legacy
``spreadsheet_write_logs`` table, its four ``target_type`` values and every
existing write path stay exactly as they are. A rename-and-backfill would stop
the previous application version from writing logs during the rollback window,
and there is no way to reconstruct an operator for rows that never recorded one.
"""

from alembic import op
import sqlalchemy as sa


revision = "0027_export_events"
down_revision = "0026_review_command_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=True, index=True),
        sa.Column("grading_batch_id", sa.String(length=36), nullable=True, index=True),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("result_revision", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("export_events")
