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


def downgrade() -> None:
    with op.batch_alter_table("export_events") as batch_op:
        batch_op.drop_constraint("uq_export_events_legacy_log", type_="unique")
        batch_op.drop_column("legacy_log_id")
