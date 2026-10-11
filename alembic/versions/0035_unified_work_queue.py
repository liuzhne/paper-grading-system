"""Unify batch-scoring execution on one database-backed work queue.

Revision ID: 0035_unified_work_queue
Revises: 0034_anthropic_messages_provider
Create Date: 2026-10-10

Vercel and the intranet/local worker used to run batch scoring through two
different code paths (per-paper queue messages vs. one worker per job).  Both
now claim one item at a time through ``claim_next_item``; this migration adds
what that function needs on ``batch_scoring_items``:

- ``source_key``/``owner_id``/``ordinal``: the model source an item counts
  against, who started it and its position inside the job (claim order is
  ``ordinal`` first, so every job's first paper runs before any job's second);
- ``heartbeat_at``/``stall_count``/``not_before``: per-item lease, consecutive
  executions without progress, and the earliest time it may run again.

Two partial indexes keep claiming, capacity counting and sweeping independent
of the table size: only pending or running rows are indexed.

``batch_scoring_jobs.last_swept_at`` rate-limits the per-job sweep that a
progress read triggers.  ``work_runtime_state`` records when the global sweep
last ran, so a progress read can revive a lost Vercel sweep chain.  It is a new
table, so the grant and RLS policy for ``pgs_app`` live here (the 0023 rule).

Existing items are backfilled from their job and batch.  The downgrade refuses
while any batch-scoring job is queued, running or cancel_requested: the old
executors cannot resume an item without these columns.
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_unified_work_queue"
down_revision = "0034_anthropic_messages_provider"
branch_labels = None
depends_on = None


ITEMS = "batch_scoring_items"
JOBS = "batch_scoring_jobs"
STATE = "work_runtime_state"
CLAIM_INDEX = "ix_batch_scoring_items_claim"
RUNNING_INDEX = "ix_batch_scoring_items_running"


def _configure_postgres_runtime_access() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
    ).scalar()
    if not role_exists:
        return
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % STATE)
    op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % STATE)
    op.execute("DROP POLICY IF EXISTS pgs_app_dml ON %s" % STATE)
    op.execute(
        "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
        "USING (true) WITH CHECK (true)" % STATE
    )


def _backfill_items() -> None:
    connection = op.get_bind()
    op.execute(
        sa.text(
            "UPDATE batch_scoring_items SET "
            "source_key = COALESCE(("
            "  SELECT 'connection:' || b.ai_connection_id "
            "  FROM batch_scoring_jobs j JOIN grading_batches b ON b.id = j.grading_batch_id "
            "  WHERE j.id = batch_scoring_items.job_id AND b.ai_connection_id IS NOT NULL"
            "), 'platform'), "
            "owner_id = ("
            "  SELECT COALESCE(j.created_by, b.owner_id) "
            "  FROM batch_scoring_jobs j JOIN grading_batches b ON b.id = j.grading_batch_id "
            "  WHERE j.id = batch_scoring_items.job_id"
            "), "
            # A running item is checked against its lease from now on; the old
            # executors only recorded when it started.
            "heartbeat_at = CASE WHEN status = 'running' THEN started_at ELSE NULL END"
        )
    )
    if connection.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE batch_scoring_items AS item SET ordinal = ranked.position - 1 "
                "FROM (SELECT id, ROW_NUMBER() OVER ("
                "  PARTITION BY job_id ORDER BY created_at, id) AS position "
                "  FROM batch_scoring_items) AS ranked "
                "WHERE ranked.id = item.id"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE batch_scoring_items SET ordinal = ("
                "  SELECT count(*) FROM batch_scoring_items AS other "
                "  WHERE other.job_id = batch_scoring_items.job_id AND ("
                "    other.created_at < batch_scoring_items.created_at OR ("
                "      other.created_at = batch_scoring_items.created_at "
                "      AND other.id < batch_scoring_items.id)))"
            )
        )


def upgrade() -> None:
    with op.batch_alter_table(ITEMS) as batch:
        batch.add_column(
            sa.Column(
                "source_key",
                sa.String(length=80),
                nullable=False,
                server_default="platform",
            )
        )
        batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column("stall_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("not_before", sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            "ck_batch_scoring_items_work_counters",
            "ordinal >= 0 AND stall_count >= 0",
        )
    _backfill_items()
    op.create_index(
        CLAIM_INDEX,
        ITEMS,
        ["source_key", "ordinal", "created_at"],
        unique=False,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        RUNNING_INDEX,
        ITEMS,
        ["source_key", "heartbeat_at"],
        unique=False,
        sqlite_where=sa.text("status = 'running'"),
        postgresql_where=sa.text("status = 'running'"),
    )
    with op.batch_alter_table(JOBS) as batch:
        batch.add_column(sa.Column("last_swept_at", sa.DateTime(), nullable=True))
    op.create_table(
        STATE,
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("value_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    _configure_postgres_runtime_access()


def downgrade() -> None:
    connection = op.get_bind()
    active = connection.execute(
        sa.text(
            "SELECT count(*) FROM batch_scoring_jobs "
            "WHERE status IN ('queued', 'running', 'cancel_requested')"
        )
    ).scalar_one()
    if active:
        raise RuntimeError(
            "0035 downgrade refused: %d batch scoring job(s) are queued, running or "
            "cancel_requested. The previous executors cannot resume their items "
            "without the work-queue columns; let them finish or cancel them first."
            % active
        )
    op.drop_table(STATE)
    with op.batch_alter_table(JOBS) as batch:
        batch.drop_column("last_swept_at")
    op.drop_index(RUNNING_INDEX, table_name=ITEMS)
    op.drop_index(CLAIM_INDEX, table_name=ITEMS)
    with op.batch_alter_table(ITEMS) as batch:
        batch.drop_constraint("ck_batch_scoring_items_work_counters", type_="check")
        batch.drop_column("not_before")
        batch.drop_column("stall_count")
        batch.drop_column("heartbeat_at")
        batch.drop_column("ordinal")
        batch.drop_column("owner_id")
        batch.drop_column("source_key")
