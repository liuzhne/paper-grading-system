"""Add organization ownership to legacy thesis resources.

Revision ID: 0019_resource_organization_scope
Revises: 0018_identity_organizations
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_resource_organization_scope"
down_revision = "0018_identity_organizations"
branch_labels = None
depends_on = None


def upgrade():
    for table in (
        "rubrics",
        "grading_batches",
        "papers",
        "scoring_runs",
        "evaluation_batches",
        "submissions",
    ):
        op.add_column(table, sa.Column("organization_id", sa.String(length=36), nullable=True))
        op.create_index("ix_%s_organization_id" % table, table, ["organization_id"])


def downgrade():
    connection = op.get_bind()
    for table in (
        "rubrics",
        "grading_batches",
        "papers",
        "scoring_runs",
        "evaluation_batches",
        "submissions",
    ):
        if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM %s WHERE organization_id IS NOT NULL)" % table)).scalar():
            raise RuntimeError("0019_resource_organization_scope downgrade would lose organization ownership")
    for table in (
        "submissions",
        "evaluation_batches",
        "scoring_runs",
        "papers",
        "grading_batches",
        "rubrics",
    ):
        op.drop_index("ix_%s_organization_id" % table, table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("organization_id")
