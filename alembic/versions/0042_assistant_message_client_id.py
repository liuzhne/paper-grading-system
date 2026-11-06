"""Assistant message client id: per-send request id the backend dedupes on.

Revision ID: 0042_assistant_message_client_id
Revises: 0041_assistant_agent_state
Create Date: 2026-11-06

The scoring assistant cannot tell whether a message reached the backend when a
request fails with a 5xx or the connection drops (docs/评分助手发送去重方案.md):
the backend commits the teacher's message before it releases the run lock and
answers.  Each send now carries a client-generated id; a second request with the
same id in the same conversation replays the first result instead of creating
another message, so resending is always safe.

``assistant_messages.client_id`` is nullable (old pages send none, assistant
messages never have one) and a partial unique index on
``(conversation_id, client_id)`` lets the database refuse a duplicate even if two
requests slip past the run lock.  Only a column and an index are added to an
existing table: the table-level ``pgs_app`` grants and RLS policies of 0038
already cover them.  The downgrade refuses while any row carries an id, because
dropping it would silently turn deduplication off for in-flight resends.
"""

from alembic import op
import sqlalchemy as sa


revision = "0042_assistant_message_client_id"
down_revision = "0041_assistant_agent_state"
branch_labels = None
depends_on = None

INDEX = "uq_assistant_messages_conversation_client_id"


def upgrade():
    op.add_column("assistant_messages", sa.Column("client_id", sa.String(64), nullable=True))
    op.create_index(
        INDEX,
        "assistant_messages",
        ["conversation_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("client_id IS NOT NULL"),
        sqlite_where=sa.text("client_id IS NOT NULL"),
    )


def downgrade():
    connection = op.get_bind()
    stored = connection.execute(
        sa.text("SELECT count(*) FROM assistant_messages WHERE client_id IS NOT NULL")
    ).scalar_one()
    if stored:
        raise RuntimeError(
            "0042 downgrade would erase %d assistant message client id(s) and turn off send deduplication; "
            "clear assistant_messages.client_id before downgrading" % stored
        )
    op.drop_index(INDEX, table_name="assistant_messages")
    with op.batch_alter_table("assistant_messages") as batch:
        batch.drop_column("client_id")
