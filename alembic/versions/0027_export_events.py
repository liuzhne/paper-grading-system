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
        "SELECT COUNT(*) FROM export_events",
        "export_events",
        "这是导出审计；丢了就没有「谁在什么时候导出了什么」。",
    )
    op.drop_table("export_events")
