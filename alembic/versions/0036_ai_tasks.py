"""AI operations become durable tasks; rules record their AI provenance.

Revision ID: 0036_ai_tasks
Revises: 0035_unified_work_queue
Create Date: 2026-10-10

Drafting deduction rules (and, in later phases, unit classification, rule
review and structure suggestion) used to call the model inside the browser's
request.  ``ai_tasks`` / ``ai_task_items`` persist the request and one row per
model call; items are claimed by the same ``claim_next_item`` as batch-scoring
items, so they share the connection's concurrency limit.

- ``ai_tasks``: the frozen input, the pinned connection (id, key version,
  snapshot) and model name, progress counters, the merged result and the root
  error.  A partial unique index on ``(rubric_id, fingerprint)`` over queued,
  running and succeeded tasks makes a repeated submission return the existing
  task instead of paying again.
- ``ai_task_items``: one model call each, with the same lease, stall and
  ``not_before`` columns and partial indexes as ``batch_scoring_items``.
- ``atomic_rules.ai_origin`` / ``ai_model``: whether a rule came from AI and
  which model generated it.  Existing rows are backfilled once from the old
  inference (AI source on the structured entry, or ``creation_method='llm'``);
  ``ai_model`` stays NULL for history, meaning "not recorded".

Both new tables get the ``pgs_app`` grant and RLS policy here (the 0023 rule).
The downgrade refuses while any AI task exists or any rule records a model:
neither can be rebuilt.
"""

import json
import re

from alembic import op
import sqlalchemy as sa


revision = "0036_ai_tasks"
down_revision = "0035_unified_work_queue"
branch_labels = None
depends_on = None


TASKS = "ai_tasks"
ITEMS = "ai_task_items"
KINDS = "('rule_draft', 'unit_classification', 'rule_review', 'structure_suggestion')"
TASK_STATUSES = "('queued', 'running', 'succeeded', 'failed', 'canceled', 'superseded')"
ITEM_STATUSES = "('pending', 'running', 'succeeded', 'failed', 'canceled')"
# 0036 时刻的 AI 来源集合（rule_origin.AI_RULE_SOURCES）；迁移里冻结，不随代码变化。
AI_RULE_SOURCES = ("ai_interpreted_user_text", "ai_inferred", "llm")


def _hex_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def _configure_postgres_runtime_access() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
    ).scalar()
    if not role_exists:
        return
    for table in (TASKS, ITEMS):
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % table)
        op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % table)
        op.execute("DROP POLICY IF EXISTS pgs_app_dml ON %s" % table)
        op.execute(
            "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
            "USING (true) WITH CHECK (true)" % table
        )


def _structured_entry(entries, rule_code, criterion_code):
    """The display-only provenance lookup of rule_origin.rule_origin at 0036."""

    explicit = next(
        (item for item in entries if isinstance(item, dict) and item.get("rule_code") == rule_code),
        None,
    )
    if explicit is not None:
        return explicit
    prefix = "manual.%s.deduct." % str(criterion_code or "").lower()
    if not rule_code.startswith(prefix):
        return {}
    match = re.fullmatch(r"(\d+)\.v1", rule_code[len(prefix):])
    index = int(match[1]) - 1 if match else -1
    if 0 <= index < len(entries) and isinstance(entries[index], dict):
        return entries[index]
    return {}


def _backfill_ai_origin() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT r.id, r.rule_code, r.creation_method, c.code, c.deduction_rules_structured "
            "FROM atomic_rules r JOIN rubric_criteria c ON c.id = r.criterion_id"
        )
    ).all()
    ai_ids = []
    for rule_id, rule_code, creation_method, criterion_code, structured in rows:
        entries = structured
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except ValueError:
                entries = []
        entries = entries if isinstance(entries, list) else []
        origin = _structured_entry(entries, str(rule_code or ""), criterion_code)
        if creation_method == "llm" or origin.get("source") in AI_RULE_SOURCES:
            ai_ids.append(rule_id)
    for start in range(0, len(ai_ids), 500):
        chunk = ai_ids[start:start + 500]
        connection.execute(
            sa.text("UPDATE atomic_rules SET ai_origin = :value WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"value": True, "ids": chunk},
        )


def upgrade() -> None:
    op.create_table(
        TASKS,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("rubric_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("ai_connection_id", sa.String(length=36), nullable=True),
        sa.Column("ai_connection_key_version", sa.Integer(), nullable=True),
        sa.Column("ai_connection_snapshot", sa.JSON(), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("running_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("canceled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_swept_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN %s" % KINDS, name="ck_ai_tasks_kind"),
        sa.CheckConstraint("status IN %s" % TASK_STATUSES, name="ck_ai_tasks_status"),
        sa.CheckConstraint(_hex_check("fingerprint"), name="ck_ai_tasks_fingerprint"),
        sa.CheckConstraint(
            "total_items >= 0 AND pending_count >= 0 AND running_count >= 0 "
            "AND succeeded_count >= 0 AND failed_count >= 0 AND canceled_count >= 0",
            name="ck_ai_tasks_nonnegative_counts",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rubric_id"], ["rubrics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ai_connection_id"], ["ai_connections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_tasks_one_live_fingerprint",
        TASKS,
        ["rubric_id", "fingerprint"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running', 'succeeded')"),
        postgresql_where=sa.text("status IN ('queued', 'running', 'succeeded')"),
    )
    op.create_index("ix_ai_tasks_rubric_kind_status", TASKS, ["rubric_id", "kind", "status"])
    op.create_index("ix_ai_tasks_organization_id", TASKS, ["organization_id"])

    op.create_table(
        ITEMS,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("not_before", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stall_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deferral_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repair_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_history", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN %s" % ITEM_STATUSES, name="ck_ai_task_items_status"),
        sa.CheckConstraint(
            "ordinal >= 0 AND attempt_count >= 0 AND stall_count >= 0 "
            "AND deferral_count >= 0 AND retry_count >= 0 AND repair_count >= 0",
            name="ck_ai_task_items_counters",
        ),
        sa.ForeignKeyConstraint(["task_id"], [TASKS + ".id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "ordinal", name="uq_ai_task_items_task_ordinal"),
    )
    op.create_index("ix_ai_task_items_task_status", ITEMS, ["task_id", "status"])
    op.create_index(
        "ix_ai_task_items_claim",
        ITEMS,
        ["source_key", "ordinal", "created_at"],
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_ai_task_items_running",
        ITEMS,
        ["source_key", "heartbeat_at"],
        sqlite_where=sa.text("status = 'running'"),
        postgresql_where=sa.text("status = 'running'"),
    )

    with op.batch_alter_table("atomic_rules") as batch:
        batch.add_column(
            sa.Column("ai_origin", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("ai_model", sa.String(length=200), nullable=True))
    _backfill_ai_origin()
    _configure_postgres_runtime_access()


def downgrade() -> None:
    connection = op.get_bind()
    tasks = connection.execute(sa.text("SELECT count(*) FROM ai_tasks")).scalar_one()
    models = connection.execute(
        sa.text("SELECT count(*) FROM atomic_rules WHERE ai_model IS NOT NULL")
    ).scalar_one()
    if tasks or models:
        raise RuntimeError(
            "0036 downgrade refused: %d AI task(s) exist and %d rule(s) record the model "
            "that generated them. Neither can be rebuilt after the downgrade; publish or "
            "delete the affected rubrics' tasks and archive the provenance first."
            % (tasks, models)
        )
    with op.batch_alter_table("atomic_rules") as batch:
        batch.drop_column("ai_model")
        batch.drop_column("ai_origin")
    op.drop_index("ix_ai_task_items_running", table_name=ITEMS)
    op.drop_index("ix_ai_task_items_claim", table_name=ITEMS)
    op.drop_index("ix_ai_task_items_task_status", table_name=ITEMS)
    op.drop_table(ITEMS)
    op.drop_index("ix_ai_tasks_organization_id", table_name=TASKS)
    op.drop_index("ix_ai_tasks_rubric_kind_status", table_name=TASKS)
    op.drop_index("ix_ai_tasks_one_live_fingerprint", table_name=TASKS)
    op.drop_table(TASKS)
