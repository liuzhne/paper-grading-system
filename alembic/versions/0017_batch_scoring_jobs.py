"""Add durable batch scoring jobs and item checkpoints.

Revision ID: 0017_batch_scoring_jobs
Revises: 0016_general_submissions
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_batch_scoring_jobs"
down_revision = "0016_general_submissions"
branch_labels = None
depends_on = None


def _lower_hex_digest_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def upgrade():
    op.create_table(
        "batch_scoring_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("grading_batch_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("rescore", sa.Boolean(), nullable=False),
        sa.Column("max_workers", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("total_items", sa.Integer(), nullable=False),
        sa.Column("pending_count", sa.Integer(), nullable=False),
        sa.Column("running_count", sa.Integer(), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("canceled_count", sa.Integer(), nullable=False),
        sa.Column("observation_policy", sa.JSON(), nullable=False),
        sa.Column("observation_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("metrics_snapshot", sa.JSON(), nullable=True),
        sa.Column("runner_token", sa.String(length=36), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "generation > 0", name="ck_batch_scoring_jobs_positive_generation"
        ),
        sa.CheckConstraint(
            "max_workers >= 1 AND max_workers <= 16",
            name="ck_batch_scoring_jobs_worker_bound",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'cancel_requested', 'completed', "
            "'completed_with_errors', 'canceled', 'failed')",
            name="ck_batch_scoring_jobs_status",
        ),
        sa.CheckConstraint(
            "total_items >= 0 AND pending_count >= 0 AND running_count >= 0 "
            "AND succeeded_count >= 0 AND skipped_count >= 0 "
            "AND failed_count >= 0 AND canceled_count >= 0",
            name="ck_batch_scoring_jobs_nonnegative_counts",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("observation_policy_hash"),
            name="ck_batch_scoring_jobs_policy_hash",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_batch_scoring_jobs_creator"
        ),
        sa.ForeignKeyConstraint(
            ["grading_batch_id"],
            ["grading_batches.id"],
            name="fk_batch_scoring_jobs_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grading_batch_id",
            "generation",
            name="uq_batch_scoring_jobs_batch_generation",
        ),
    )
    op.create_index(
        "ix_batch_scoring_jobs_one_active_per_batch",
        "batch_scoring_jobs",
        ["grading_batch_id"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('queued', 'running', 'cancel_requested')"
        ),
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'cancel_requested')"
        ),
    )
    op.create_index(
        "ix_batch_scoring_jobs_batch_created",
        "batch_scoring_jobs",
        ["grading_batch_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "batch_scoring_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("scoring_run_id", sa.String(length=36), nullable=True),
        sa.Column("baseline_scoring_run_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("telemetry", sa.JSON(), nullable=True),
        sa.Column("attempt_history", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'skipped', "
            "'failed', 'canceled')",
            name="ck_batch_scoring_items_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_batch_scoring_items_nonnegative_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["batch_scoring_jobs.id"],
            name="fk_batch_scoring_items_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["papers.id"],
            name="fk_batch_scoring_items_paper",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_scoring_run_id"],
            ["scoring_runs.id"],
            name="fk_batch_scoring_items_baseline_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"],
            ["scoring_runs.id"],
            name="fk_batch_scoring_items_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "paper_id", name="uq_batch_scoring_items_job_paper"
        ),
    )
    op.create_index(
        "ix_batch_scoring_items_job_status",
        "batch_scoring_items",
        ["job_id", "status"],
        unique=False,
    )


def downgrade():
    connection = op.get_bind()
    counts = {
        table: int(
            connection.execute(
                sa.text("SELECT count(*) FROM %s" % table)
            ).scalar_one()
        )
        for table in ("batch_scoring_jobs", "batch_scoring_items")
    }
    if any(counts.values()):
        raise RuntimeError(
            "0017 downgrade would lose durable batch scoring state "
            "(%s)"
            % ", ".join(
                "%s=%s" % (key, value)
                for key, value in sorted(counts.items())
            )
        )

    op.drop_index(
        "ix_batch_scoring_items_job_status", table_name="batch_scoring_items"
    )
    op.drop_table("batch_scoring_items")
    op.drop_index(
        "ix_batch_scoring_jobs_batch_created", table_name="batch_scoring_jobs"
    )
    op.drop_index(
        "ix_batch_scoring_jobs_one_active_per_batch",
        table_name="batch_scoring_jobs",
    )
    op.drop_table("batch_scoring_jobs")
