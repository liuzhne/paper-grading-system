"""Let a structure-suggestion task exist before its rubric does.

Revision ID: 0037_ai_task_upload_scope
Revises: 0036_ai_tasks
Create Date: 2026-10-10

Structure recognition also runs before an import: the uploaded table failed to
parse (E1/E7), so there is no rubric yet.  Moving that call into an AI task
needs ``ai_tasks.rubric_id`` to be nullable for exactly that kind:

- ``ck_ai_tasks_rubric_scope``: only ``structure_suggestion`` may omit the
  rubric; every other kind keeps the old guarantee.
- ``ix_ai_tasks_one_live_upload_fingerprint``: the live-fingerprint lock for
  those tasks is per owner (NULL rubrics never collide in the 0036 index).

No new table, so no new grant or RLS policy.  The downgrade refuses while any
task without a rubric exists: the NOT NULL could not be restored over it.
"""

from alembic import op
import sqlalchemy as sa


revision = "0037_ai_task_upload_scope"
down_revision = "0036_ai_tasks"
branch_labels = None
depends_on = None


TASKS = "ai_tasks"
SCOPE_CHECK = "ck_ai_tasks_rubric_scope"
UPLOAD_INDEX = "ix_ai_tasks_one_live_upload_fingerprint"
LIVE = "status IN ('queued', 'running', 'succeeded')"


def upgrade() -> None:
    # SQLite cannot drop NOT NULL in place; batch mode rebuilds the table and keeps
    # the other constraints and the partial indexes (asserted in test_migrations).
    with op.batch_alter_table(TASKS) as batch:
        batch.alter_column("rubric_id", existing_type=sa.String(length=36), nullable=True)
        batch.create_check_constraint(
            SCOPE_CHECK, "rubric_id IS NOT NULL OR kind = 'structure_suggestion'"
        )
    op.create_index(
        UPLOAD_INDEX,
        TASKS,
        ["owner_id", "fingerprint"],
        unique=True,
        sqlite_where=sa.text("rubric_id IS NULL AND %s" % LIVE),
        postgresql_where=sa.text("rubric_id IS NULL AND %s" % LIVE),
    )


def downgrade() -> None:
    connection = op.get_bind()
    orphans = connection.execute(
        sa.text("SELECT count(*) FROM ai_tasks WHERE rubric_id IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            "0037 downgrade refused: %d structure-suggestion task(s) were created before "
            "their rubric existed (import preview). Delete them first; rubric_id cannot be "
            "made NOT NULL over them." % orphans
        )
    op.drop_index(UPLOAD_INDEX, table_name=TASKS)
    with op.batch_alter_table(TASKS) as batch:
        batch.drop_constraint(SCOPE_CHECK, type_="check")
        batch.alter_column("rubric_id", existing_type=sa.String(length=36), nullable=False)
