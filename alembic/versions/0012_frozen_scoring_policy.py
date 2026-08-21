"""M1：冻结评分 policy、单项聚合和人工 resolution 身份。

Revision ID: 0012_frozen_scoring_policy
Revises: 0011_version_hash_on_update
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_frozen_scoring_policy"
down_revision = "0011_version_hash_on_update"
branch_labels = None
depends_on = None


_RUN_CHECK = "ck_scoring_runs_policy_identity"
_ITEM_CHECK = "ck_score_items_aggregation_state"
_REVIEW_CHECK = "ck_review_logs_policy_resolution"


def _lower_hex_digest_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def _json_null(column_name):
    # Reflected SQLAlchemy JSON columns do not retain ``none_as_null=True`` and
    # may bind Python None as the JSON literal ``null``.  Treat it exactly like
    # SQL NULL at the database boundary while still rejecting partial identity.
    return "(%s IS NULL OR CAST(%s AS TEXT) = 'null')" % (
        column_name,
        column_name,
    )


def _json_present(column_name):
    return "(%s IS NOT NULL AND CAST(%s AS TEXT) <> 'null')" % (
        column_name,
        column_name,
    )


def upgrade():
    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "policy_snapshot",
                sa.JSON(none_as_null=True),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("policy_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("policy_schema_version", sa.String(length=100), nullable=True)
        )
        batch_op.create_check_constraint(
            _RUN_CHECK,
            "(%s AND policy_hash IS NULL AND policy_schema_version IS NULL) "
            "OR (%s AND policy_hash IS NOT NULL "
            "AND policy_schema_version IS NOT NULL AND length(policy_schema_version) > 0 "
            "AND %s)"
            % (
                _json_null("policy_snapshot"),
                _json_present("policy_snapshot"),
                _lower_hex_digest_check("policy_hash"),
            ),
        )

    with op.batch_alter_table("score_items") as batch_op:
        batch_op.alter_column(
            "ai_score",
            existing_type=sa.Numeric(6, 2),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column("aggregation", sa.JSON(none_as_null=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("aggregation_schema_version", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("auto_score_status", sa.String(length=50), nullable=True)
        )
        batch_op.create_check_constraint(
            _ITEM_CHECK,
            "(%s AND aggregation_schema_version IS NULL "
            "AND auto_score_status IS NULL AND ai_score IS NOT NULL) "
            "OR (%s AND aggregation_schema_version IS NOT NULL "
            "AND length(aggregation_schema_version) > 0 "
            "AND auto_score_status IS NOT NULL "
            "AND auto_score_status IN ('calculated', 'invalid', 'blocked') "
            "AND ((auto_score_status = 'calculated' AND ai_score IS NOT NULL) "
            "OR (auto_score_status IN ('invalid', 'blocked') AND ai_score IS NULL)))"
            % (_json_null("aggregation"), _json_present("aggregation")),
        )

    with op.batch_alter_table("review_logs") as batch_op:
        batch_op.add_column(sa.Column("policy_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("resolution_type", sa.String(length=50), nullable=True)
        )
        batch_op.create_check_constraint(
            _REVIEW_CHECK,
            "(policy_hash IS NULL AND resolution_type IS NULL) "
            "OR (policy_hash IS NOT NULL AND %s "
            "AND resolution_type IS NOT NULL "
            "AND resolution_type IN ('ordinary_override', 'resolve_validation', "
            "'resolve_block', 'rescore'))" % _lower_hex_digest_check("policy_hash"),
        )


def downgrade():
    connection = op.get_bind()
    unsafe = any(
        connection.scalar(sa.text(statement)) is not None
        for statement in (
            "SELECT 1 FROM scoring_runs "
            "WHERE (policy_snapshot IS NOT NULL "
            "AND CAST(policy_snapshot AS TEXT) <> 'null') OR policy_hash IS NOT NULL "
            "OR policy_schema_version IS NOT NULL LIMIT 1",
            "SELECT 1 FROM score_items "
            "WHERE (aggregation IS NOT NULL AND CAST(aggregation AS TEXT) <> 'null') "
            "OR aggregation_schema_version IS NOT NULL "
            "OR auto_score_status IS NOT NULL OR ai_score IS NULL LIMIT 1",
            "SELECT 1 FROM review_logs "
            "WHERE policy_hash IS NOT NULL OR resolution_type IS NOT NULL LIMIT 1",
        )
    )
    if unsafe:
        raise RuntimeError(
            "downgrade to 0011 would lose M1 policy/aggregation/resolution data; "
            "export the affected scoring runs before downgrade"
        )

    # 所有安全检查都在任何 DDL 之前完成，失败时 schema 与数据保持原样。
    with op.batch_alter_table("review_logs") as batch_op:
        batch_op.drop_constraint(_REVIEW_CHECK, type_="check")
        batch_op.drop_column("resolution_type")
        batch_op.drop_column("policy_hash")

    with op.batch_alter_table("score_items") as batch_op:
        batch_op.drop_constraint(_ITEM_CHECK, type_="check")
        batch_op.drop_column("auto_score_status")
        batch_op.drop_column("aggregation_schema_version")
        batch_op.drop_column("aggregation")
        batch_op.alter_column(
            "ai_score",
            existing_type=sa.Numeric(6, 2),
            nullable=False,
        )

    with op.batch_alter_table("scoring_runs") as batch_op:
        batch_op.drop_constraint(_RUN_CHECK, type_="check")
        batch_op.drop_column("policy_schema_version")
        batch_op.drop_column("policy_hash")
        batch_op.drop_column("policy_snapshot")
