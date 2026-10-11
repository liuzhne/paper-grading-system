"""Add the scoring assistant's preferences, conversations, messages and graph state.

Revision ID: 0038_assistant_conversations
Revises: 0037_ai_task_upload_scope
Create Date: 2026-10-11

Five new tables, so the 0023 rule applies: grant ``pgs_app`` and enable RLS
in the migration itself.  Production runs as ``pgs_app`` without DDL rights;
forgetting the grant does not fail the migration, it makes the application
fail with "permission denied" right after a successful deploy.

The downgrade refuses while any table has rows: conversations are user data,
not a cache that can be rebuilt (same policy as 0024–0028 and 0031).

`assistant_checkpoints` / `assistant_checkpoint_writes` back the custom
LangGraph checkpointer (design doc T3).  The official Postgres saver creates
its own tables at runtime, which the production role cannot do; keeping the
schema in Alembic gives one code path for SQLite tests and Postgres.
"""

from alembic import op
import sqlalchemy as sa


revision = "0038_assistant_conversations"
down_revision = "0037_ai_task_upload_scope"
branch_labels = None
depends_on = None


TABLES = (
    "assistant_preferences",
    "assistant_conversations",
    "assistant_messages",
    "assistant_checkpoints",
    "assistant_checkpoint_writes",
)


def _configure_postgres_runtime_access():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
    ).scalar()
    if not role_exists:
        return
    for table in TABLES:
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % table
        )
        op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % table)
        op.execute(
            "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
            "USING (true) WITH CHECK (true)" % table
        )


def upgrade():
    op.create_table(
        "assistant_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("model_source", sa.String(length=20), nullable=False),
        sa.Column("ai_connection_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "model_source IN ('connection', 'platform')",
            name="ck_assistant_preferences_model_source",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["ai_connection_id"], ["ai_connections.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "organization_id", name="uq_assistant_preferences_user_org"
        ),
    )
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("focus", sa.JSON(), nullable=False),
        sa.Column("thread_id", sa.String(length=100), nullable=True),
        sa.Column("flow_seq", sa.Integer(), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=True),
        sa.Column("run_locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_conversations_owner_updated",
        "assistant_conversations",
        ["owner_id", "organization_id", "updated_at"],
    )
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("cards", sa.JSON(), nullable=False),
        sa.Column("intent", sa.String(length=50), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_assistant_messages_role",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["assistant_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_messages_conversation_created",
        "assistant_messages",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "assistant_checkpoints",
        sa.Column("thread_id", sa.String(length=100), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_type", sa.String(length=50), nullable=False),
        sa.Column("checkpoint_data", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_type", sa.String(length=50), nullable=False),
        sa.Column("metadata_data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
    )
    op.create_table(
        "assistant_checkpoint_writes",
        sa.Column("thread_id", sa.String(length=100), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=255), nullable=False),
        sa.Column("value_type", sa.String(length=50), nullable=False),
        sa.Column("value_data", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    )
    _configure_postgres_runtime_access()


def downgrade():
    connection = op.get_bind()
    for table in TABLES:
        if connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % table)
        ).scalar():
            raise RuntimeError(
                "0038_assistant_conversations downgrade would drop user data in %s; "
                "delete the conversations explicitly before downgrading" % table
            )
    op.drop_table("assistant_checkpoint_writes")
    op.drop_table("assistant_checkpoints")
    op.drop_index(
        "ix_assistant_messages_conversation_created", table_name="assistant_messages"
    )
    op.drop_table("assistant_messages")
    op.drop_index(
        "ix_assistant_conversations_owner_updated",
        table_name="assistant_conversations",
    )
    op.drop_table("assistant_conversations")
    op.drop_table("assistant_preferences")
