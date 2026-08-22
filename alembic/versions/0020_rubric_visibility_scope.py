"""Add explicit rubric visibility scope.

Revision ID: 0020_rubric_visibility_scope
Revises: 0019_resource_organization_scope
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_rubric_visibility_scope"
down_revision = "0019_resource_organization_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rubrics",
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="private"),
    )
    op.add_column(
        "rubric_versions",
        sa.Column("organization_id", sa.String(length=36), nullable=True),
    )
    op.add_column("rubrics", sa.Column("published_by", sa.String(length=36), nullable=True))
    op.add_column("rubrics", sa.Column("archived_by", sa.String(length=36), nullable=True))
    op.create_index(
        "ix_rubric_versions_organization_id",
        "rubric_versions",
        ["organization_id"],
    )
    op.execute(
        "UPDATE rubric_versions SET organization_id = "
        "(SELECT organization_id FROM rubrics WHERE rubrics.id = rubric_versions.rubric_id)"
    )
    with op.batch_alter_table("rubrics") as batch:
        batch.drop_constraint("uq_rubrics_name_version", type_="unique")
    for name, columns, clause in (
        ("uq_rubrics_system_name_version", ["name", "version"], "visibility = 'system'"),
        ("uq_rubrics_organization_name_version", ["organization_id", "name", "version"], "visibility = 'organization'"),
        ("uq_rubrics_private_name_version", ["owner_id", "name", "version"], "visibility = 'private'"),
    ):
        op.create_index(name, "rubrics", columns, unique=True, sqlite_where=sa.text(clause), postgresql_where=sa.text(clause))


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM rubrics WHERE visibility <> 'private')")).scalar():
        raise RuntimeError("0020_rubric_visibility_scope downgrade would lose rubric visibility")
    for name in (
        "uq_rubrics_private_name_version",
        "uq_rubrics_organization_name_version",
        "uq_rubrics_system_name_version",
    ):
        op.drop_index(name, table_name="rubrics")
    op.drop_index("ix_rubric_versions_organization_id", table_name="rubric_versions")
    with op.batch_alter_table("rubric_versions") as batch:
        batch.drop_column("organization_id")
    with op.batch_alter_table("rubrics") as batch:
        batch.drop_column("archived_by")
        batch.drop_column("published_by")
        batch.drop_column("visibility")
        batch.create_unique_constraint("uq_rubrics_name_version", ["name", "version"])
