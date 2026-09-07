"""Add idempotency receipts for bulk review commands.

Revision ID: 0026_review_command_receipts
Revises: 0025_review_contract
Create Date: 2026-09-07

Frontend v2 plan §5-B. A retried bulk acceptance must return the original
result rather than writing a second set of ReviewLog rows. The unique key is
scoped by organization and actor so two tenants cannot collide on a key, and
one user's retry cannot replay another user's command.
"""

from alembic import op
import sqlalchemy as sa


revision = "0026_review_command_receipts"
down_revision = "0025_review_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_command_receipts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("command", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "actor_id",
            "command",
            "idempotency_key",
            name="uq_review_command_receipts_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("review_command_receipts")
