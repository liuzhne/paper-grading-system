"""Add generic evaluation batches, submissions, and document snapshots.

Revision ID: 0016_general_submissions
Revises: 0015_scoring_run_runtime_identity
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_general_submissions"
down_revision = "0015_scoring_run_runtime_identity"
branch_labels = None
depends_on = None


_RUN_TARGET_CHECK = "ck_scoring_runs_exactly_one_target"
_RUN_SNAPSHOT_CHECK = "ck_scoring_runs_submission_snapshot_target"
_RUN_SUBMISSION_FK = "fk_scoring_runs_submission"
_RUN_DOCUMENT_FK = "fk_scoring_runs_document_snapshot_submission"


def _lower_hex_digest_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def upgrade():
    op.create_table(
        "evaluation_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("rubric_id", sa.String(length=36), nullable=False),
        sa.Column("rubric_version_id", sa.String(length=36), nullable=False),
        sa.Column("business_profile_key", sa.String(length=100), nullable=False),
        sa.Column(
            "business_profile_version", sa.String(length=100), nullable=False
        ),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'archived')",
            name="ck_evaluation_batches_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_evaluation_batches_creator"
        ),
        sa.ForeignKeyConstraint(
            ["rubric_id"], ["rubrics.id"], name="fk_evaluation_batches_rubric"
        ),
        sa.ForeignKeyConstraint(
            ["rubric_version_id", "rubric_id"],
            ["rubric_versions.id", "rubric_versions.rubric_id"],
            name="fk_evaluation_batches_rubric_version_rubric",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_batch_id", sa.String(length=36), nullable=False),
        sa.Column("source_artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("source_artifact_ref", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _lower_hex_digest_check("source_artifact_hash"),
            name="ck_submissions_source_artifact_hash",
        ),
        sa.CheckConstraint(
            "byte_length >= 0", name="ck_submissions_nonnegative_byte_length"
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'parsing', 'parsed', 'failed', "
            "'scored', 'pending_review', 'reviewed')",
            name="ck_submissions_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_submissions_creator"
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_batch_id"],
            ["evaluation_batches.id"],
            name="fk_submissions_evaluation_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_submissions_evaluation_batch_id",
        "submissions",
        ["evaluation_batch_id"],
    )

    op.create_table(
        "document_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("submission_id", sa.String(length=36), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("business_profile_key", sa.String(length=100), nullable=False),
        sa.Column(
            "business_profile_version", sa.String(length=100), nullable=False
        ),
        sa.Column("parser_version", sa.String(length=100), nullable=False),
        sa.Column("normalizer_version", sa.String(length=100), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_ref", sa.Text(), nullable=False),
        sa.Column("snapshot_payload", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _lower_hex_digest_check("content_hash"),
            name="ck_document_snapshots_content_hash",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("snapshot_hash"),
            name="ck_document_snapshots_snapshot_hash",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_document_snapshots_submission",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "submission_id",
            name="uq_document_snapshots_id_submission",
        ),
        sa.UniqueConstraint(
            "submission_id",
            "snapshot_hash",
            name="uq_document_snapshots_submission_hash",
        ),
    )
    op.create_index(
        "ix_document_snapshots_submission_id",
        "document_snapshots",
        ["submission_id"],
    )

    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.alter_column(
            "paper_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column("submission_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("document_snapshot_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            _RUN_SUBMISSION_FK,
            "submissions",
            ["submission_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            _RUN_DOCUMENT_FK,
            "document_snapshots",
            ["document_snapshot_id", "submission_id"],
            ["id", "submission_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            _RUN_TARGET_CHECK,
            "(paper_id IS NOT NULL AND submission_id IS NULL) OR "
            "(paper_id IS NULL AND submission_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            _RUN_SNAPSHOT_CHECK,
            "(submission_id IS NULL AND document_snapshot_id IS NULL) OR "
            "(submission_id IS NOT NULL AND document_snapshot_id IS NOT NULL)",
        )


def downgrade():
    connection = op.get_bind()
    counts = {
        table: int(
            connection.execute(
                sa.text("SELECT count(*) FROM %s" % table)
            ).scalar_one()
        )
        for table in (
            "evaluation_batches",
            "submissions",
            "document_snapshots",
        )
    }
    submission_runs = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM scoring_runs WHERE submission_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if any(counts.values()) or submission_runs:
        details = ", ".join(
            "%s=%s" % (key, value) for key, value in sorted(counts.items())
        )
        raise RuntimeError(
            "0016 downgrade would lose general submission data "
            "(%s, submission_runs=%s)" % (details, submission_runs)
        )

    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.drop_constraint(_RUN_SNAPSHOT_CHECK, type_="check")
        batch_op.drop_constraint(_RUN_TARGET_CHECK, type_="check")
        batch_op.drop_constraint(_RUN_DOCUMENT_FK, type_="foreignkey")
        batch_op.drop_constraint(_RUN_SUBMISSION_FK, type_="foreignkey")
        batch_op.drop_column("document_snapshot_id")
        batch_op.drop_column("submission_id")
        batch_op.alter_column(
            "paper_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )

    op.drop_index(
        "ix_document_snapshots_submission_id",
        table_name="document_snapshots",
    )
    op.drop_table("document_snapshots")
    op.drop_index("ix_submissions_evaluation_batch_id", table_name="submissions")
    op.drop_table("submissions")
    op.drop_table("evaluation_batches")
