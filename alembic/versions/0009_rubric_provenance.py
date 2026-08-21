"""P1-01：评分标准来源、编译运行与不可变版本。

Revision ID: 0009_rubric_provenance
Revises: 0008_owner_id
Create Date: 2026-07-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_rubric_provenance"
down_revision = "0008_owner_id"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rubric_compilations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("rubric_id", sa.String(length=36), sa.ForeignKey("rubrics.id"), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("parser_version", sa.String(length=100), nullable=False),
        sa.Column("compiler_version", sa.String(length=100), nullable=False),
        sa.Column("model_provider", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("sampling_params", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("raw_parse_output", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("raw_model_output", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("validation_result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("blockers", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("human_changes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("final_version_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("id", "rubric_id", name="uq_rubric_compilations_id_rubric"),
        sa.UniqueConstraint("id", "final_version_hash", name="uq_rubric_compilations_id_final_hash"),
    )

    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "compilation_id",
            sa.String(length=36),
            sa.ForeignKey("rubric_compilations.id"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("file_size_bytes >= 0", name="ck_source_artifacts_nonnegative_size"),
    )

    op.create_table(
        "source_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "source_artifact_id",
            sa.String(length=36),
            sa.ForeignKey("source_artifacts.id"),
            nullable=False,
        ),
        sa.Column("source_rule_code", sa.String(length=50), nullable=False),
        sa.Column("sheet_name", sa.String(length=200), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("cell_locator", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("row_number >= 1", name="ck_source_rules_positive_row"),
    )

    op.create_table(
        "rubric_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("rubric_id", sa.String(length=36), nullable=False),
        sa.Column("compilation_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("workflow_profile", sa.String(length=100), nullable=False),
        sa.Column("global_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["compilation_id", "rubric_id"],
            ["rubric_compilations.id", "rubric_compilations.rubric_id"],
            name="fk_rubric_versions_compilation_rubric",
        ),
        sa.ForeignKeyConstraint(
            ["compilation_id", "version_hash"],
            ["rubric_compilations.id", "rubric_compilations.final_version_hash"],
            name="fk_rubric_versions_compilation_hash",
        ),
        sa.UniqueConstraint("compilation_id", name="uq_rubric_versions_compilation"),
        sa.UniqueConstraint("rubric_id", "version", name="uq_rubric_versions_rubric_version"),
    )

    op.create_index("ix_rubric_compilations_rubric_id", "rubric_compilations", ["rubric_id"])
    op.create_index("ix_source_artifacts_compilation_id", "source_artifacts", ["compilation_id"])
    op.create_index("ix_source_rules_source_artifact_id", "source_rules", ["source_artifact_id"])


def downgrade():
    op.drop_index("ix_source_rules_source_artifact_id", table_name="source_rules")
    op.drop_index("ix_source_artifacts_compilation_id", table_name="source_artifacts")
    op.drop_index("ix_rubric_compilations_rubric_id", table_name="rubric_compilations")
    op.drop_table("rubric_versions")
    op.drop_table("source_rules")
    op.drop_table("source_artifacts")
    op.drop_table("rubric_compilations")
