"""ScoringRun.coherence_findings（篇章一致性发现，设计§8）。

新增列带 server_default，安全回填存量行。

Revision ID: 0003_coherence_findings
Revises: 0002_scoring_alignment
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_coherence_findings"
down_revision = "0002_scoring_alignment"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scoring_runs",
        sa.Column("coherence_findings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade():
    op.drop_column("scoring_runs", "coherence_findings")
