"""Assistant Agent state: per-message turn metadata and the conversation compaction.

Revision ID: 0041_assistant_agent_state
Revises: 0040_provider_output_observations
Create Date: 2026-10-22

The scoring assistant becomes a workflow plus a single Agent
(docs/评分助手Agent化方案.md, phase A0).  Two pieces of state have no home in
the 0038 tables:

- ``assistant_messages.meta``: what an Agent turn did -- the deferred tools it
  loaded for the first time and the tools it called (the history renders a
  load record from the former, and compaction keeps tools that are still in
  use from the latter), the turn mode (agent / fast / degraded) and the prefix
  identity used to spot a deploy or model switch;
- ``assistant_conversations.compaction``: the compacted older turns -- facts
  put together by code, the teacher's requirements summarized by the model,
  the still-used tools, and which message the compaction covers (§12.4).

Only nullable columns are added: the table-level ``pgs_app`` grants and RLS
policies of 0038 already cover them.  The downgrade refuses while any row
carries a value, because neither can be rebuilt afterwards.
"""

from alembic import op
import sqlalchemy as sa


revision = "0041_assistant_agent_state"
down_revision = "0040_provider_output_observations"
branch_labels = None
depends_on = None


COLUMNS = (
    ("assistant_messages", "meta"),
    ("assistant_conversations", "compaction"),
)


def upgrade():
    for table, column in COLUMNS:
        op.add_column(table, sa.Column(column, sa.JSON(), nullable=True))


def downgrade():
    connection = op.get_bind()
    for table, column in COLUMNS:
        stored = connection.execute(
            sa.text("SELECT count(*) FROM %s WHERE %s IS NOT NULL" % (table, column))
        ).scalar_one()
        if stored:
            raise RuntimeError(
                "0041 downgrade would erase assistant Agent state in %d %s row(s); "
                "clear %s.%s before downgrading" % (stored, table, table, column)
            )
    for table, column in reversed(COLUMNS):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
