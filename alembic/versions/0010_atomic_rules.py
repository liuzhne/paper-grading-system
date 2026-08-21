"""P1-02：模板条目、原子规则、档位和模板映射。

Revision ID: 0010_atomic_rules
Revises: 0009_rubric_provenance
Create Date: 2026-07-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_atomic_rules"
down_revision = "0009_rubric_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "template_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "source_artifact_id",
            sa.String(length=36),
            sa.ForeignKey("source_artifacts.id"),
            nullable=False,
        ),
        sa.Column("item_code", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("section_path", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized_constraint", sa.JSON(), nullable=True),
        sa.Column("strictness", sa.String(length=50), nullable=False),
        sa.Column("source_locator", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("parse_confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('section', 'content', 'comment', 'structure', 'format')",
            name="ck_template_items_kind",
        ),
        sa.CheckConstraint(
            "strictness IN ('required', 'preferred', 'unknown')",
            name="ck_template_items_strictness",
        ),
        sa.CheckConstraint(
            "parse_confidence >= 0 AND parse_confidence <= 1",
            name="ck_template_items_parse_confidence",
        ),
        sa.UniqueConstraint(
            "source_artifact_id",
            "item_code",
            name="uq_template_items_artifact_code",
        ),
    )

    op.create_table(
        "atomic_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "rubric_version_id",
            sa.String(length=36),
            sa.ForeignKey("rubric_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "criterion_id",
            sa.String(length=36),
            sa.ForeignKey("rubric_criteria.id"),
            nullable=False,
        ),
        sa.Column("rule_code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("direction", sa.String(length=50), nullable=False),
        sa.Column("effect_type", sa.String(length=50), nullable=False),
        sa.Column("max_points", sa.Numeric(8, 2), nullable=True),
        sa.Column("repeat_policy", sa.String(length=50), nullable=True),
        sa.Column("cap_points", sa.Numeric(8, 2), nullable=True),
        sa.Column("judge_type", sa.String(length=50), nullable=False),
        sa.Column("checker_key", sa.String(length=200), nullable=True),
        sa.Column("checker_params", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("evidence_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("positive_example", sa.Text(), nullable=True),
        sa.Column("negative_example", sa.Text(), nullable=True),
        sa.Column("boundary_example", sa.Text(), nullable=True),
        sa.Column("strictness", sa.String(length=50), nullable=False),
        sa.Column("applies_to", sa.Text(), nullable=False),
        sa.Column("mutex_group", sa.String(length=100), nullable=True),
        sa.Column(
            "depends_on_rule_codes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("creation_method", sa.String(length=50), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('band', 'deduct', 'bonus', 'none')",
            name="ck_atomic_rules_direction",
        ),
        sa.CheckConstraint(
            "effect_type IN ('score', 'review', 'block_submission', 'report_only')",
            name="ck_atomic_rules_effect_type",
        ),
        sa.CheckConstraint(
            "repeat_policy IS NULL OR repeat_policy IN ('once', 'per_occurrence', 'capped')",
            name="ck_atomic_rules_repeat_policy",
        ),
        sa.CheckConstraint(
            "judge_type IN ('deterministic', 'semantic')",
            name="ck_atomic_rules_judge_type",
        ),
        sa.CheckConstraint(
            "strictness IN ('required', 'preferred', 'unknown')",
            name="ck_atomic_rules_strictness",
        ),
        sa.CheckConstraint(
            "max_points IS NULL OR max_points >= 0",
            name="ck_atomic_rules_nonnegative_max_points",
        ),
        sa.CheckConstraint(
            "cap_points IS NULL OR cap_points >= 0",
            name="ck_atomic_rules_nonnegative_cap_points",
        ),
        sa.UniqueConstraint(
            "rubric_version_id",
            "rule_code",
            name="uq_atomic_rules_version_code",
        ),
    )

    op.create_table(
        "atomic_rule_source_rules",
        sa.Column(
            "atomic_rule_id",
            sa.String(length=36),
            sa.ForeignKey("atomic_rules.id"),
            nullable=False,
        ),
        sa.Column(
            "source_rule_id",
            sa.String(length=36),
            sa.ForeignKey("source_rules.id"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "atomic_rule_id",
            "source_rule_id",
            name="pk_atomic_rule_source_rules",
        ),
    )

    op.create_table(
        "rule_levels",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "atomic_rule_id",
            sa.String(length=36),
            sa.ForeignKey("atomic_rules.id"),
            nullable=False,
        ),
        sa.Column("level_code", sa.String(length=100), nullable=False),
        sa.Column("points", sa.Numeric(8, 2), nullable=False),
        sa.Column("descriptor", sa.Text(), nullable=False),
        sa.Column("positive_example", sa.Text(), nullable=True),
        sa.Column("negative_example", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("points >= 0", name="ck_rule_levels_nonnegative_points"),
        sa.CheckConstraint(
            "display_order >= 0",
            name="ck_rule_levels_nonnegative_display_order",
        ),
        sa.UniqueConstraint(
            "atomic_rule_id",
            "level_code",
            name="uq_rule_levels_rule_code",
        ),
    )

    op.create_table(
        "rule_template_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "rule_id",
            sa.String(length=36),
            sa.ForeignKey("atomic_rules.id"),
            nullable=False,
        ),
        sa.Column(
            "template_item_id",
            sa.String(length=36),
            sa.ForeignKey("template_items.id"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.String(length=50), nullable=False),
        sa.Column("match_method", sa.String(length=50), nullable=False),
        sa.Column("match_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(length=50), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "relationship_type IN ('support', 'constraint', 'format_baseline', 'exception')",
            name="ck_rule_template_links_relationship_type",
        ),
        sa.CheckConstraint(
            "match_method IN ('exact', 'heuristic', 'llm_suggestion', 'manual')",
            name="ck_rule_template_links_match_method",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'rejected')",
            name="ck_rule_template_links_review_status",
        ),
        sa.CheckConstraint(
            "match_confidence IS NULL OR "
            "(match_confidence >= 0 AND match_confidence <= 1)",
            name="ck_rule_template_links_match_confidence",
        ),
        sa.UniqueConstraint(
            "rule_id",
            "template_item_id",
            name="uq_rule_template_links_rule_item",
        ),
    )

    op.create_index(
        "ix_template_items_source_artifact_id",
        "template_items",
        ["source_artifact_id"],
    )
    op.create_index(
        "ix_atomic_rules_rubric_version_id",
        "atomic_rules",
        ["rubric_version_id"],
    )
    op.create_index("ix_atomic_rules_criterion_id", "atomic_rules", ["criterion_id"])
    op.create_index("ix_rule_levels_atomic_rule_id", "rule_levels", ["atomic_rule_id"])
    op.create_index("ix_rule_template_links_rule_id", "rule_template_links", ["rule_id"])
    op.create_index(
        "ix_rule_template_links_template_item_id",
        "rule_template_links",
        ["template_item_id"],
    )


def downgrade():
    op.drop_index(
        "ix_rule_template_links_template_item_id",
        table_name="rule_template_links",
    )
    op.drop_index("ix_rule_template_links_rule_id", table_name="rule_template_links")
    op.drop_index("ix_rule_levels_atomic_rule_id", table_name="rule_levels")
    op.drop_index("ix_atomic_rules_criterion_id", table_name="atomic_rules")
    op.drop_index("ix_atomic_rules_rubric_version_id", table_name="atomic_rules")
    op.drop_index(
        "ix_template_items_source_artifact_id",
        table_name="template_items",
    )
    op.drop_table("rule_template_links")
    op.drop_table("rule_levels")
    op.drop_table("atomic_rule_source_rules")
    op.drop_table("atomic_rules")
    op.drop_table("template_items")
