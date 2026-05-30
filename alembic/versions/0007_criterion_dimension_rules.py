"""RubricCriterion.dimension + deduction_rules_structured（设计§2/§3.2/§5）。

Revision ID: 0007_criterion_dimension_rules
Revises: 0006_format_findings
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_criterion_dimension_rules"
down_revision = "0006_format_findings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("rubric_criteria", sa.Column("dimension", sa.String(length=50), nullable=True))
    op.add_column(
        "rubric_criteria",
        sa.Column("deduction_rules_structured", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade():
    op.drop_column("rubric_criteria", "deduction_rules_structured")
    op.drop_column("rubric_criteria", "dimension")
