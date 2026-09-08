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


def _refuse_if_rows(query, what, restore):
    """有数据时拒绝有损降级（前端 v2 计划 §7）。

    这些不是缓存，删掉重建不回来。空库降级仍然允许——回滚一个刚上线还没产生
    数据的版本是正常操作，把它一并堵死会逼人去手工删表。
    """
    count = op.get_bind().execute(sa.text(query)).scalar() or 0
    if count:
        raise RuntimeError(
            "拒绝有损降级：%s 仍有 %d 条记录，降级会永久删除它们。\n"
            "%s" % (what, count, restore)
        )

def downgrade() -> None:
    _refuse_if_rows(
        "SELECT COUNT(*) FROM review_command_receipts",
        "review_command_receipts",
        "这是批量采纳的幂等回执；丢了会让一次重放变成二次写入。",
    )
    op.drop_table("review_command_receipts")
