"""ScoringRun.format_findings（格式问题清单，设计§9）。

Revision ID: 0006_format_findings
Revises: 0005_rubric_format_spec
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_format_findings"
down_revision = "0005_rubric_format_spec"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scoring_runs",
        sa.Column("format_findings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade():
    op.drop_column("scoring_runs", "format_findings")
