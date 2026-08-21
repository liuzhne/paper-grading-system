"""Freeze profile, prompt, and runtime identity on Core scoring runs.

Revision ID: 0015_scoring_run_runtime_identity
Revises: 0014_release_gate_profiles
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_scoring_run_runtime_identity"
down_revision = "0014_release_gate_profiles"
branch_labels = None
depends_on = None


_CHECK = "ck_scoring_runs_core_replay_identity"
_RUNTIME_COLUMNS = (
    "business_profile_version",
    "prompt_version",
    "runtime_identity",
)
_BASE_COLUMNS = (
    "rubric_source_kind",
    "rubric_snapshot_hash",
    "rubric_version_id",
    "rubric_version_hash",
    "rubric_hash_scheme",
    "business_profile_key",
    "workflow_profile",
    "execution_plan_snapshot",
    "execution_plan_hash",
    "plan_schema_version",
    "checker_manifest",
    "source_artifact_hash",
    "normalized_content_hash",
    "document_snapshot_ref",
    "document_snapshot_hash",
    "document_schema_version",
    "engine_version",
    "rescore_generation",
    "idempotency_key",
)


def _widen_alembic_version_column():
    """Allow revision identifiers longer than Alembic's default VARCHAR(32).

    SQLite does not enforce the declared VARCHAR length and does not support
    the direct ALTER COLUMN form.  Production PostgreSQL does enforce it, so
    widen the control table before Alembic records this 33-character revision.
    The wider type is intentionally retained on downgrade for later revisions.
    """

    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=False,
        )


def _lower_hex_digest_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def _identity_check(*, include_runtime):
    columns = _BASE_COLUMNS + (_RUNTIME_COLUMNS if include_runtime else ())
    historical = " AND ".join("%s IS NULL" % name for name in columns)
    digests = {
        name: _lower_hex_digest_check(name)
        for name in (
            "rubric_snapshot_hash",
            "execution_plan_hash",
            "source_artifact_hash",
            "normalized_content_hash",
            "document_snapshot_hash",
            "idempotency_key",
            "policy_hash",
            "rubric_version_hash",
        )
    }
    runtime = (
        "AND ((business_profile_version IS NULL AND prompt_version IS NULL "
        "AND runtime_identity IS NULL) OR (business_profile_version IS NOT NULL "
        "AND length(business_profile_version) > 0 AND prompt_version IS NOT NULL "
        "AND length(prompt_version) > 0 AND runtime_identity IS NOT NULL)) "
        if include_runtime
        else ""
    )
    core = (
        "rubric_source_kind IS NOT NULL "
        "AND rubric_source_kind IN ('published_version', 'legacy_unversioned') "
        "AND rubric_snapshot_hash IS NOT NULL AND {rubric_snapshot_hash} "
        "AND business_profile_key IS NOT NULL AND length(business_profile_key) > 0 "
        "{runtime}"
        "AND workflow_profile IS NOT NULL AND length(workflow_profile) > 0 "
        "AND execution_plan_snapshot IS NOT NULL "
        "AND execution_plan_hash IS NOT NULL AND {execution_plan_hash} "
        "AND plan_schema_version IS NOT NULL AND length(plan_schema_version) > 0 "
        "AND checker_manifest IS NOT NULL "
        "AND source_artifact_hash IS NOT NULL AND {source_artifact_hash} "
        "AND normalized_content_hash IS NOT NULL AND {normalized_content_hash} "
        "AND document_snapshot_ref IS NOT NULL AND length(document_snapshot_ref) > 0 "
        "AND document_snapshot_hash IS NOT NULL AND {document_snapshot_hash} "
        "AND document_schema_version IS NOT NULL AND length(document_schema_version) > 0 "
        "AND engine_version IS NOT NULL AND length(engine_version) > 0 "
        "AND rescore_generation IS NOT NULL AND rescore_generation >= 0 "
        "AND idempotency_key IS NOT NULL AND {idempotency_key} "
        "AND policy_snapshot IS NOT NULL "
        "AND policy_hash IS NOT NULL AND {policy_hash} "
        "AND policy_schema_version IS NOT NULL AND length(policy_schema_version) > 0 "
        "AND ((rubric_source_kind = 'legacy_unversioned' "
        "AND rubric_version_id IS NULL AND rubric_version_hash IS NULL "
        "AND rubric_hash_scheme IS NULL) OR (rubric_source_kind = 'published_version' "
        "AND rubric_version_id IS NOT NULL AND rubric_version_hash IS NOT NULL "
        "AND {rubric_version_hash} AND rubric_hash_scheme IS NOT NULL "
        "AND length(rubric_hash_scheme) > 0))"
    ).format(runtime=runtime, **digests)
    return "(%s) OR (%s)" % (historical, core)


def upgrade():
    _widen_alembic_version_column()
    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.add_column(
            sa.Column(
                "business_profile_version",
                sa.String(length=100),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("prompt_version", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "runtime_identity",
                sa.JSON(none_as_null=True),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            _CHECK,
            _identity_check(include_runtime=True),
        )


def downgrade():
    connection = op.get_bind()
    populated = connection.scalar(
        sa.text(
            "SELECT 1 FROM scoring_runs WHERE "
            "business_profile_version IS NOT NULL OR prompt_version IS NOT NULL "
            "OR runtime_identity IS NOT NULL LIMIT 1"
        )
    )
    if populated is not None:
        raise RuntimeError(
            "downgrade to 0014 would lose frozen runtime identity; "
            "archive scoring runs before downgrade"
        )

    # Restoring 0013's original check is safe because all new columns are empty.
    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        for column_name in reversed(_RUNTIME_COLUMNS):
            batch_op.drop_column(column_name)
        batch_op.create_check_constraint(
            _CHECK,
            _identity_check(include_runtime=False),
        )
