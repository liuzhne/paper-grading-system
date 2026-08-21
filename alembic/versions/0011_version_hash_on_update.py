"""P1-03：允许发布时原子更新内容哈希。

Revision ID: 0011_version_hash_on_update
Revises: 0010_atomic_rules
Create Date: 2026-07-17
"""

from alembic import op


revision = "0011_version_hash_on_update"
down_revision = "0010_atomic_rules"
branch_labels = None
depends_on = None


_CONSTRAINT = "fk_rubric_versions_compilation_hash"


def _replace_hash_foreign_key(*, onupdate=None):
    # batch_alter_table 同时支持 SQLite 的重建表路径与 PostgreSQL 的 ALTER。
    with op.batch_alter_table("rubric_versions") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="foreignkey")
        batch_op.create_foreign_key(
            _CONSTRAINT,
            "rubric_compilations",
            ["compilation_id", "version_hash"],
            ["id", "final_version_hash"],
            onupdate=onupdate,
        )


def upgrade():
    _replace_hash_foreign_key(onupdate="CASCADE")


def downgrade():
    _replace_hash_foreign_key()
