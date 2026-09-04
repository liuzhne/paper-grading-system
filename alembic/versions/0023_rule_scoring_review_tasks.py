"""Add rule checkpoints and claimable manual review tasks.

Revision ID: 0023_rule_scoring_review_tasks
Revises: 0022_legacy_tenant_backfill
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "0023_rule_scoring_review_tasks"
down_revision = "0022_legacy_tenant_backfill"
branch_labels = None
depends_on = None


def _configure_postgres_runtime_access():
    """Match the production least-privilege role when that role exists.

    Local/CI databases do not create ``pgs_app``.  Supabase production does,
    and new tables must not depend on an out-of-band grant/policy step.
    Tenant filtering remains enforced by application queries, consistent with
    the existing production policy baseline.
    """

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
    ).scalar()
    if not role_exists:
        return
    for table in ("rule_scoring_tasks", "manual_review_tasks"):
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % table
        )
        op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % table)
        op.execute(
            "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
            "USING (true) WITH CHECK (true)" % table
        )


def upgrade():
    op.create_table(
        "rule_scoring_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("scoring_run_id", sa.String(length=36), nullable=False),
        sa.Column("score_item_id", sa.String(length=36), nullable=True),
        sa.Column("criterion_code", sa.String(length=100), nullable=False),
        sa.Column("rule_code", sa.String(length=200), nullable=False),
        sa.Column("judge_type", sa.String(length=50), nullable=False),
        sa.Column("dependency_rule_codes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("blocking_final_total", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("provider_error", sa.JSON(), nullable=True),
        sa.Column("result_snapshot", sa.JSON(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'retry_wait', "
            "'failed_exhausted', 'skipped', 'review_required', 'canceled', "
            "'deferred_provider')",
            name="ck_rule_scoring_tasks_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name="ck_rule_scoring_tasks_attempts",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"], ["scoring_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["score_item_id"], ["score_items.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scoring_run_id", "rule_code", name="uq_rule_scoring_tasks_run_rule"
        ),
    )
    op.create_index(
        "ix_rule_scoring_tasks_run_status",
        "rule_scoring_tasks",
        ["scoring_run_id", "status"],
    )
    op.create_index(
        "ix_rule_scoring_tasks_org_status",
        "rule_scoring_tasks",
        ["organization_id", "status"],
    )

    op.create_table(
        "manual_review_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("scoring_run_id", sa.String(length=36), nullable=False),
        sa.Column("rule_scoring_task_id", sa.String(length=36), nullable=True),
        sa.Column("score_item_id", sa.String(length=36), nullable=True),
        sa.Column("criterion_code", sa.String(length=100), nullable=False),
        sa.Column("rule_code", sa.String(length=200), nullable=True),
        sa.Column("trigger_code", sa.String(length=100), nullable=False),
        sa.Column("trigger_message", sa.Text(), nullable=False),
        sa.Column("blocking_final_total", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("assigned_reviewer_id", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_type", sa.String(length=50), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolution_evidence", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'claimed', 'resolved', 'canceled', 'superseded')",
            name="ck_manual_review_tasks_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_manual_review_tasks_version"),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_manual_review_tasks_priority",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"], ["scoring_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rule_scoring_task_id"],
            ["rule_scoring_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["score_item_id"], ["score_items.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["assigned_reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_scoring_task_id", name="uq_manual_review_tasks_rule_task"
        ),
    )
    op.create_index(
        "ix_manual_review_tasks_org_status",
        "manual_review_tasks",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_manual_review_tasks_run_status",
        "manual_review_tasks",
        ["scoring_run_id", "status"],
    )
    _configure_postgres_runtime_access()


def downgrade():
    connection = op.get_bind()
    for table in ("manual_review_tasks", "rule_scoring_tasks"):
        if connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % table)
        ).scalar():
            raise RuntimeError(
                "0023_rule_scoring_review_tasks downgrade would lose workflow audit history"
            )
    op.drop_index("ix_manual_review_tasks_run_status", table_name="manual_review_tasks")
    op.drop_index("ix_manual_review_tasks_org_status", table_name="manual_review_tasks")
    op.drop_table("manual_review_tasks")
    op.drop_index("ix_rule_scoring_tasks_org_status", table_name="rule_scoring_tasks")
    op.drop_index("ix_rule_scoring_tasks_run_status", table_name="rule_scoring_tasks")
    op.drop_table("rule_scoring_tasks")
