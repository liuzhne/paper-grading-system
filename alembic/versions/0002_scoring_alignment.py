"""Scoring alignment: structured deductions, criterion semantics, token metering.

对齐《设计方案》：
- rubric_criteria 原子项语义（criterion_type/scoring_mode/applies_to/rubric_levels/sub_checks，§2）
- score_items 结构化扣分/分档选档/hybrid 子检查（deduction_items/band_selection/sub_results，§2/§6.3/N6）
- scoring_runs Token/成本计量（§10.4）

新增列均带 server_default，保证存量行安全回填。

Revision ID: 0002_scoring_alignment
Revises: 0001_initial_core
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_scoring_alignment"
down_revision = "0001_initial_core"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rubric_criteria",
        sa.Column("criterion_type", sa.String(length=20), nullable=False, server_default="llm_judgment"),
    )
    op.add_column(
        "rubric_criteria",
        sa.Column("scoring_mode", sa.String(length=20), nullable=False, server_default="llm_direct"),
    )
    op.add_column(
        "rubric_criteria",
        sa.Column("applies_to", sa.String(length=100), nullable=False, server_default="global"),
    )
    op.add_column(
        "rubric_criteria",
        sa.Column("rubric_levels", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "rubric_criteria",
        sa.Column("sub_checks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )

    op.add_column(
        "score_items",
        sa.Column("deduction_items", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("score_items", sa.Column("band_selection", sa.JSON(), nullable=True))
    op.add_column("score_items", sa.Column("sub_results", sa.JSON(), nullable=True))

    op.add_column("scoring_runs", sa.Column("prompt_tokens", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("scoring_runs", sa.Column("completion_tokens", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("scoring_runs", sa.Column("total_tokens", sa.Integer(), nullable=True, server_default="0"))


def downgrade():
    for table, column in [
        ("scoring_runs", "total_tokens"),
        ("scoring_runs", "completion_tokens"),
        ("scoring_runs", "prompt_tokens"),
        ("score_items", "sub_results"),
        ("score_items", "band_selection"),
        ("score_items", "deduction_items"),
        ("rubric_criteria", "sub_checks"),
        ("rubric_criteria", "rubric_levels"),
        ("rubric_criteria", "applies_to"),
        ("rubric_criteria", "scoring_mode"),
        ("rubric_criteria", "criterion_type"),
    ]:
        op.drop_column(table, column)
