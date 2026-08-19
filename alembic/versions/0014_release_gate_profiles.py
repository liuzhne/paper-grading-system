"""Database-backed release gate profiles, candidates, and approvals.

Revision ID: 0014_release_gate_profiles
Revises: 0013_core_replay_identity
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_release_gate_profiles"
down_revision = "0013_core_replay_identity"
branch_labels = None
depends_on = None


def _lower_hex_digest_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def upgrade():
    op.create_table(
        "release_gate_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gate_key", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("rubric_id", sa.String(length=36), nullable=False),
        sa.Column("rubric_version_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_identity", sa.JSON(), nullable=False),
        sa.Column("model_identity", sa.JSON(), nullable=False),
        sa.Column("anchors_identity", sa.JSON(), nullable=False),
        sa.Column("acceptance_thresholds", sa.JSON(), nullable=False),
        sa.Column("regression_tolerances", sa.JSON(), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "gate_key IN ('GATE-01', 'GATE-02', 'GATE-03')",
            name="ck_release_gate_profiles_gate_key",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_release_gate_profiles_status",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("profile_hash"),
            name="ck_release_gate_profiles_hash",
        ),
        sa.ForeignKeyConstraint(
            ["rubric_version_id", "rubric_id"],
            ["rubric_versions.id", "rubric_versions.rubric_id"],
            name="fk_release_gate_profiles_rubric_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_release_gate_profiles_created_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gate_key", "name", name="uq_release_gate_profiles_gate_name"
        ),
        sa.UniqueConstraint(
            "profile_hash", name="uq_release_gate_profiles_hash"
        ),
    )

    op.create_table(
        "release_gate_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("profile_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_id", sa.String(length=200), nullable=False),
        sa.Column("candidate_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_record", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("final_record", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("final_record_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            _lower_hex_digest_check("candidate_sha256"),
            name="ck_release_gate_runs_candidate_hash",
        ),
        sa.CheckConstraint(
            "final_record_sha256 IS NULL OR "
            + _lower_hex_digest_check("final_record_sha256"),
            name="ck_release_gate_runs_final_hash",
        ),
        sa.CheckConstraint(
            "status IN ('candidate_awaiting_approval', 'passed', "
            "'failed_thresholds', 'ineligible')",
            name="ck_release_gate_runs_status",
        ),
        sa.CheckConstraint(
            "(status = 'candidate_awaiting_approval' AND final_record IS NULL "
            "AND final_record_sha256 IS NULL AND finalized_at IS NULL) OR "
            "(status <> 'candidate_awaiting_approval' AND final_record IS NOT NULL "
            "AND final_record_sha256 IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_release_gate_runs_final_state",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["release_gate_profiles.id"],
            name="fk_release_gate_runs_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "candidate_sha256", name="uq_release_gate_runs_id_candidate"
        ),
        sa.UniqueConstraint(
            "evaluation_id", name="uq_release_gate_runs_evaluation_id"
        ),
        sa.UniqueConstraint(
            "candidate_sha256", name="uq_release_gate_runs_candidate"
        ),
    )
    op.create_index(
        "ix_release_gate_runs_profile_id",
        "release_gate_runs",
        ["profile_id"],
    )

    op.create_table(
        "release_gate_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_sha256", sa.String(length=64), nullable=False),
        sa.Column("privacy_review", sa.JSON(), nullable=False),
        sa.Column("accepted_baseline", sa.JSON(), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _lower_hex_digest_check("candidate_sha256"),
            name="ck_release_gate_approvals_candidate_hash",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "candidate_sha256"],
            ["release_gate_runs.id", "release_gate_runs.candidate_sha256"],
            name="fk_release_gate_approvals_exact_candidate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_release_gate_approvals_approved_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_release_gate_approvals_run"),
    )


def downgrade():
    connection = op.get_bind()
    for table_name in (
        "release_gate_approvals",
        "release_gate_runs",
        "release_gate_profiles",
    ):
        if connection.scalar(
            sa.text("SELECT 1 FROM %s LIMIT 1" % table_name)
        ) is not None:
            raise RuntimeError(
                "downgrade to 0013 would lose release gate records; "
                "export or retire them before downgrade"
            )

    op.drop_table("release_gate_approvals")
    op.drop_index(
        "ix_release_gate_runs_profile_id", table_name="release_gate_runs"
    )
    op.drop_table("release_gate_runs")
    op.drop_table("release_gate_profiles")
