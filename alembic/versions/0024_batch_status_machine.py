"""Constrain grading batch stages and add optimistic state versioning.

Revision ID: 0024_batch_status_machine
Revises: 0023_rule_scoring_review_tasks
Create Date: 2026-09-07

Frontend v2 plan §5-C / §7.

``grading_batches.status`` had no CHECK constraint, and the application only
ever wrote four of the seven designed stages.  "Only four values are written"
is not the same as "only four values exist": earlier releases let clients set
``status`` freely through POST/PATCH, so production may hold anything.

This migration therefore surveys the live values first and **fails closed** on
anything it cannot explain, rather than quietly rewriting unknown rows to
``draft`` — a batch silently reset to draft would lose the fact that it had
already been scored and reviewed.
"""

from alembic import op
import sqlalchemy as sa


revision = "0024_batch_status_machine"
down_revision = "0023_rule_scoring_review_tasks"
branch_labels = None
depends_on = None


VALID_STAGES = (
    "draft",
    "parsing",
    "scoring",
    "scored",
    "scored_with_errors",
    "reviewed",
    "archived",
)

#: Legacy values seen in older code paths, mapped to their designed stage.
#: Every entry is a documented rename, never a guess.
KNOWN_RENAMES = {
    # 0011-era vocabulary shared with EvaluationBatch before the two models
    # diverged; "active" meant "a scoring pass is running".
    "active": "scoring",
    "completed": "scored",
}


def _survey_unknown_values(connection):
    rows = connection.execute(
        sa.text("SELECT status, COUNT(*) AS total FROM grading_batches GROUP BY status")
    ).mappings()
    unknown = {}
    for row in rows:
        value = row["status"]
        if value in VALID_STAGES or value in KNOWN_RENAMES:
            continue
        unknown[value] = int(row["total"])
    return unknown


def upgrade() -> None:
    connection = op.get_bind()

    unknown = _survey_unknown_values(connection)
    if unknown:
        # Report counts only — never batch names or ids, which carry course and
        # department information.
        summary = ", ".join(
            "%r×%d" % (value, count) for value, count in sorted(unknown.items())
        )
        raise RuntimeError(
            "grading_batches.status holds values this migration cannot map: "
            + summary
            + ". Resolve them deliberately (see docs/前端v2改造计划.md §5-C) and "
            "re-run; the migration refuses to guess a stage for them."
        )

    for legacy, target in KNOWN_RENAMES.items():
        connection.execute(
            sa.text("UPDATE grading_batches SET status = :target WHERE status = :legacy"),
            {"target": target, "legacy": legacy},
        )

    # batch_alter_table covers both SQLite's table-rebuild path and PostgreSQL's
    # native ALTER; SQLite supports neither ADD CONSTRAINT nor DROP DEFAULT.
    # The server default backfills existing rows and is kept afterwards: the
    # application always supplies the value, and a residual DB-level default of
    # 1 is harmless while removing it costs a second table rebuild.
    with op.batch_alter_table("grading_batches") as batch_op:
        batch_op.add_column(
            sa.Column("state_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_check_constraint(
            "ck_grading_batches_state_version_positive", "state_version >= 1"
        )
        batch_op.create_check_constraint(
            "ck_grading_batches_status",
            "status IN ('draft', 'parsing', 'scoring', 'scored', "
            "'scored_with_errors', 'reviewed', 'archived')",
        )


def downgrade() -> None:
    """Drop the guards. The stage vocabulary itself is left intact.

    Renamed rows are not restored: 'active'/'completed' were ambiguous, and
    re-introducing them would put the database back into the state this
    migration exists to remove.
    """
    with op.batch_alter_table("grading_batches") as batch_op:
        batch_op.drop_constraint("ck_grading_batches_status", type_="check")
        batch_op.drop_constraint(
            "ck_grading_batches_state_version_positive", type_="check"
        )
        batch_op.drop_column("state_version")
