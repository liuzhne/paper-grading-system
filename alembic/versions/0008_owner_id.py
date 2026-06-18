"""预留 owner_id（单租户起步，为将来多用户铺路；§P4.3）。

在 rubrics / grading_batches / papers / scoring_runs 加可空 owner_id。
单租户暂不按 owner 强隔离；新建实体时回填当前用户（默认 DEFAULT_DEV_USER_ID）。

Revision ID: 0008_owner_id
Revises: 0007_criterion_dimension_rules
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_owner_id"
down_revision = "0007_criterion_dimension_rules"
branch_labels = None
depends_on = None

_TABLES = ("rubrics", "grading_batches", "papers", "scoring_runs")


def upgrade():
    for table in _TABLES:
        op.add_column(table, sa.Column("owner_id", sa.String(length=36), nullable=True))


def downgrade():
    for table in _TABLES:
        op.drop_column(table, "owner_id")
