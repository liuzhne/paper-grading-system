"""Rubric.format_spec（模板期望格式规格，设计§2/§9 前置）。

Revision ID: 0005_rubric_format_spec
Revises: 0004_calibration_anchors
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_rubric_format_spec"
down_revision = "0004_calibration_anchors"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rubrics",
        sa.Column("format_spec", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade():
    op.drop_column("rubrics", "format_spec")
