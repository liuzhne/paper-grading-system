"""Allow Claude (Anthropic Messages) as a private AI connection protocol.

Revision ID: 0034_anthropic_messages_provider
Revises: 0033_rule_decision_ledger
Create Date: 2026-10-09

Only ``ck_ai_connections_provider_type`` changes; no table is created, so the
0023 rule (grant + RLS for ``pgs_app``) does not apply.  ``platform_llm_config``
has no CHECK on ``provider_type`` (the service layer validates it), but it is
still guarded on downgrade: the old code cannot build a scorer for it.

The downgrade refuses while any connection uses the new protocol — including
soft-deleted rows, because the narrower CHECK could not be recreated over them
— or while the platform model does.  Deleting a user's key material is an
operator decision, never a migration side effect.
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_anthropic_messages_provider"
down_revision = "0033_rule_decision_ledger"
branch_labels = None
depends_on = None


CONSTRAINT = "ck_ai_connections_provider_type"
NEW_PROTOCOL = "anthropic_messages"
WITH_CLAUDE = "provider_type IN ('openai_responses', 'openai_compatible', 'anthropic_messages')"
WITHOUT_CLAUDE = "provider_type IN ('openai_responses', 'openai_compatible')"


def _replace_check(condition: str) -> None:
    # SQLite cannot alter a CHECK in place; batch mode rebuilds the table and
    # keeps the other constraints and indexes (asserted in test_migrations).
    with op.batch_alter_table("ai_connections") as batch:
        batch.drop_constraint(CONSTRAINT, type_="check")
        batch.create_check_constraint(CONSTRAINT, condition)


def upgrade() -> None:
    _replace_check(WITH_CLAUDE)


def downgrade() -> None:
    connection = op.get_bind()
    connections = connection.execute(
        sa.text("SELECT count(*) FROM ai_connections WHERE provider_type = :protocol"),
        {"protocol": NEW_PROTOCOL},
    ).scalar_one()
    platform = connection.execute(
        sa.text("SELECT count(*) FROM platform_llm_config WHERE provider_type = :protocol"),
        {"protocol": NEW_PROTOCOL},
    ).scalar_one()
    if connections or platform:
        raise RuntimeError(
            "0034 downgrade refused: %d AI connection(s) (including soft-deleted ones) "
            "and %d platform model configuration(s) use the anthropic_messages protocol. "
            "Hard-delete those connections and switch the platform model to another "
            "protocol before downgrading." % (connections, platform)
        )
    _replace_check(WITHOUT_CLAUDE)
