"""Persist structured review reasons and per-item review revisions.

Revision ID: 0025_review_contract
Revises: 0024_batch_status_machine
Create Date: 2026-09-07

Frontend v2 plan §5-B / §7.

Two additions, both additive and nullable so historical rows stay readable:

``score_items.review_reasons``
    Structured ``[{source, code, message, rule_code}]``. Historical rows keep
    NULL — they are **not** re-scored to backfill a reason, which would rewrite
    authoritative results for a cosmetic column.

``score_items.review_revision``
    Optimistic-concurrency cursor for review writes. Two reviewers acting on the
    same item must not silently overwrite each other, and a stale page must not
    be able to "confirm" a score that has since been changed.
"""

from alembic import op
import sqlalchemy as sa


revision = "0025_review_contract"
down_revision = "0024_batch_status_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("score_items") as batch_op:
        batch_op.add_column(sa.Column("review_reasons", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("review_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "review_revision", sa.Integer(), nullable=False, server_default="1"
            )
        )
        batch_op.create_check_constraint(
            "ck_score_items_review_revision_positive", "review_revision >= 1"
        )


def _refuse_if_rows(query, what, restore):
    """有数据时拒绝有损降级（前端 v2 计划 §7）。

    这些不是缓存，删掉重建不回来。空库降级仍然允许——回滚一个刚上线还没产生
    数据的版本是正常操作，把它一并堵死会逼人去手工删表。
    """
    count = op.get_bind().execute(sa.text(query)).scalar() or 0
    if count:
        raise RuntimeError(
            "拒绝有损降级：%s 仍有 %d 条记录，降级会永久删除它们。\n"
            "%s" % (what, count, restore)
        )

def downgrade() -> None:
    _refuse_if_rows(
        "SELECT COUNT(*) FROM score_items "
        "WHERE review_reason IS NOT NULL OR review_reasons IS NOT NULL",
        "score_items 的复核原因",
        "这是复核者当时看到的理由，删掉之后无法从任何地方重建；"
        "确需降级请先导出这些行并由维护者确认。",
    )
    with op.batch_alter_table("score_items") as batch_op:
        batch_op.drop_constraint(
            "ck_score_items_review_revision_positive", type_="check"
        )
        batch_op.drop_column("review_revision")
        batch_op.drop_column("review_reason")
        batch_op.drop_column("review_reasons")
