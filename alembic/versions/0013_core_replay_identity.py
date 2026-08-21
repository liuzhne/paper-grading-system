"""M3：版本化执行计划、文档快照与幂等重放身份。

Revision ID: 0013_core_replay_identity
Revises: 0012_frozen_scoring_policy
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_core_replay_identity"
down_revision = "0012_frozen_scoring_policy"
branch_labels = None
depends_on = None


_VERSION_RUBRIC_UNIQUE = "uq_rubric_versions_id_rubric"
_VERSION_IDENTITY_UNIQUE = "uq_rubric_versions_replay_identity"
_BATCH_VERSION_FK = "fk_grading_batches_rubric_version_rubric"
_RUN_VERSION_FK = "fk_scoring_runs_rubric_version_identity"
_RUN_IDENTITY_CHECK = "ck_scoring_runs_core_replay_identity"
_RUN_IDEMPOTENCY_UNIQUE = "uq_scoring_runs_idempotency_key"
_ITEM_RULE_RESULTS_CHECK = "ck_score_items_rule_results_identity"


_RUN_COLUMNS = (
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


def _lower_hex_digest_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def _core_replay_identity_check():
    historical = " AND ".join("%s IS NULL" % name for name in _RUN_COLUMNS)
    digest_names = (
        "rubric_snapshot_hash",
        "execution_plan_hash",
        "source_artifact_hash",
        "normalized_content_hash",
        "document_snapshot_hash",
        "idempotency_key",
        "policy_hash",
        "rubric_version_hash",
    )
    digest_checks = {
        name: _lower_hex_digest_check(name) for name in digest_names
    }
    core = (
        "rubric_source_kind IS NOT NULL "
        "AND rubric_source_kind IN ('published_version', 'legacy_unversioned') "
        "AND rubric_snapshot_hash IS NOT NULL AND {rubric_snapshot_hash} "
        "AND business_profile_key IS NOT NULL AND length(business_profile_key) > 0 "
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
        "AND rubric_hash_scheme IS NULL) "
        "OR (rubric_source_kind = 'published_version' "
        "AND rubric_version_id IS NOT NULL "
        "AND rubric_version_hash IS NOT NULL AND {rubric_version_hash} "
        "AND rubric_hash_scheme IS NOT NULL AND length(rubric_hash_scheme) > 0))"
    ).format(**digest_checks)
    return "(%s) OR (%s)" % (historical, core)


def upgrade():
    # Existing versions are the pre-M3 thesis/v1 population.  Add nullable,
    # backfill explicitly, then remove nullability without retaining defaults.
    with op.batch_alter_table("rubric_versions") as batch_op:
        batch_op.add_column(
            sa.Column("business_profile_key", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("hash_scheme", sa.String(length=100), nullable=True)
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE rubric_versions "
            "SET business_profile_key = 'thesis', "
            "hash_scheme = 'rubric-content-v1'"
        )
    )

    with op.batch_alter_table("rubric_versions") as batch_op:
        batch_op.alter_column(
            "business_profile_key",
            existing_type=sa.String(length=100),
            nullable=False,
        )
        batch_op.alter_column(
            "hash_scheme",
            existing_type=sa.String(length=100),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            _VERSION_RUBRIC_UNIQUE, ["id", "rubric_id"]
        )
        batch_op.create_unique_constraint(
            _VERSION_IDENTITY_UNIQUE,
            ["id", "rubric_id", "version_hash", "hash_scheme"],
        )

    with op.batch_alter_table("grading_batches") as batch_op:
        batch_op.add_column(
            sa.Column("rubric_version_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            _BATCH_VERSION_FK,
            "rubric_versions",
            ["rubric_version_id", "rubric_id"],
            ["id", "rubric_id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.add_column(
            sa.Column("rubric_source_kind", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("rubric_snapshot_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("rubric_version_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("rubric_version_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("rubric_hash_scheme", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("business_profile_key", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workflow_profile", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "execution_plan_snapshot",
                sa.JSON(none_as_null=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("execution_plan_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("plan_schema_version", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("checker_manifest", sa.JSON(none_as_null=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("source_artifact_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("normalized_content_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("document_snapshot_ref", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("document_snapshot_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("document_schema_version", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("engine_version", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(sa.Column("rescore_generation", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("idempotency_key", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            _RUN_IDENTITY_CHECK, _core_replay_identity_check()
        )
        batch_op.create_unique_constraint(
            _RUN_IDEMPOTENCY_UNIQUE, ["idempotency_key"]
        )
        batch_op.create_foreign_key(
            _RUN_VERSION_FK,
            "rubric_versions",
            [
                "rubric_version_id",
                "rubric_id",
                "rubric_version_hash",
                "rubric_hash_scheme",
            ],
            ["id", "rubric_id", "version_hash", "hash_scheme"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("score_items") as batch_op:
        batch_op.add_column(
            sa.Column("rule_results", sa.JSON(none_as_null=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "rule_results_schema_version",
                sa.String(length=100),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            _ITEM_RULE_RESULTS_CHECK,
            "(rule_results IS NULL AND rule_results_schema_version IS NULL) "
            "OR (rule_results IS NOT NULL "
            "AND rule_results_schema_version IS NOT NULL "
            "AND length(rule_results_schema_version) > 0)",
        )


def downgrade():
    connection = op.get_bind()
    unsafe_statements = (
        "SELECT 1 FROM rubric_versions "
        "WHERE business_profile_key <> 'thesis' "
        "OR hash_scheme <> 'rubric-content-v1' LIMIT 1",
        "SELECT 1 FROM grading_batches WHERE rubric_version_id IS NOT NULL LIMIT 1",
        "SELECT 1 FROM scoring_runs WHERE "
        + " OR ".join("%s IS NOT NULL" % name for name in _RUN_COLUMNS)
        + " LIMIT 1",
        "SELECT 1 FROM score_items WHERE rule_results IS NOT NULL "
        "OR rule_results_schema_version IS NOT NULL LIMIT 1",
    )
    if any(
        connection.scalar(sa.text(statement)) is not None
        for statement in unsafe_statements
    ):
        raise RuntimeError(
            "downgrade to 0012 would lose M3 version/profile/plan/document/"
            "idempotency data; export the affected records before downgrade"
        )

    # 所有可表达性检查都发生在第一条 DDL 之前；拒绝降级不会留下半迁移状态。
    with op.batch_alter_table("score_items") as batch_op:
        batch_op.drop_constraint(_ITEM_RULE_RESULTS_CHECK, type_="check")
        batch_op.drop_column("rule_results_schema_version")
        batch_op.drop_column("rule_results")

    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.drop_constraint(_RUN_VERSION_FK, type_="foreignkey")
        batch_op.drop_constraint(_RUN_IDEMPOTENCY_UNIQUE, type_="unique")
        batch_op.drop_constraint(_RUN_IDENTITY_CHECK, type_="check")
        for column_name in reversed(_RUN_COLUMNS):
            batch_op.drop_column(column_name)

    with op.batch_alter_table("grading_batches") as batch_op:
        batch_op.drop_constraint(_BATCH_VERSION_FK, type_="foreignkey")
        batch_op.drop_column("rubric_version_id")

    with op.batch_alter_table("rubric_versions") as batch_op:
        batch_op.drop_constraint(_VERSION_IDENTITY_UNIQUE, type_="unique")
        batch_op.drop_constraint(_VERSION_RUBRIC_UNIQUE, type_="unique")
        batch_op.drop_column("hash_scheme")
        batch_op.drop_column("business_profile_key")
