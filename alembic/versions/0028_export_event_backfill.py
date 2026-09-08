"""Link export events back to the legacy log rows they were backfilled from.

Revision ID: 0028_export_event_backfill
Revises: 0027_export_events
Create Date: 2026-09-08

Frontend v2 plan §5-F / §7 (stage 6B). The unique ``legacy_log_id`` is what
makes the backfill re-entrant: re-running it skips rows already carried over,
and the history projection can hide a legacy row once its event exists so one
export never shows up twice.

Re-entrancy matters because ``alembic upgrade head`` will not re-run a
completed migration. During a rollback window the previous application version
writes only to the legacy table; moving forward again must be able to pick that
stretch up, which is why the backfill lives in a callable service rather than
inside this migration.
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_export_event_backfill"
down_revision = "0027_export_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("export_events") as batch_op:
        batch_op.add_column(sa.Column("legacy_log_id", sa.String(length=36), nullable=True))
        batch_op.create_unique_constraint("uq_export_events_legacy_log", ["legacy_log_id"])


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
        "SELECT COUNT(*) FROM export_events WHERE legacy_log_id IS NOT NULL",
        "已补录的旧导出日志映射",
        "丢掉 legacy_log_id 之后重新前进会把同一条旧日志再补录一次，"
        "历史里会出现重复的导出记录。",
    )
    with op.batch_alter_table("export_events") as batch_op:
        batch_op.drop_constraint("uq_export_events_legacy_log", type_="unique")
        batch_op.drop_column("legacy_log_id")
