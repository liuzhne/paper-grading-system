"""L2 跨文档校准锚点表（设计§7）。

Revision ID: 0004_calibration_anchors
Revises: 0003_coherence_findings
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_calibration_anchors"
down_revision = "0003_coherence_findings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calibration_anchors",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("rubric_id", sa.String(length=36), sa.ForeignKey("rubrics.id"), nullable=False),
        sa.Column("criterion_code", sa.String(length=50), nullable=False),
        sa.Column("score", sa.Numeric(6, 2), nullable=False),
        sa.Column("max_score", sa.Numeric(6, 2), nullable=False),
        sa.Column("label", sa.String(length=50), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="范文"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_calibration_anchors_rubric_code", "calibration_anchors", ["rubric_id", "criterion_code"])


def downgrade():
    op.drop_index("ix_calibration_anchors_rubric_code", table_name="calibration_anchors")
    op.drop_table("calibration_anchors")
