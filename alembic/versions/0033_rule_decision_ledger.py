"""Add the rule-level decision ledger (reuse validated semantic decisions).

Revision ID: 0033_rule_decision_ledger
Revises: 0032_single_active_ai_connection
Create Date: 2026-10-05

Retries used to re-pay every rule of a paper even when most decisions had
already succeeded.  This table lets the scoring executor reuse a validated
decision whose full decision identity (document, rule, evidence selection,
provider identity, prompt version) is unchanged.

The table is a cache: its rows are reproducible by scoring again, so the
downgrade drops it even when it holds data (unlike 0024-0028/0031, whose rows
are audit records that cannot be rebuilt).

``rule_scoring_tasks.decision_reused`` and ``group_call_id`` are different:
they record which persisted decisions were replayed rather than judged, and
which mutex-group tiers came from one provider call.  Those facts cannot be
rebuilt, so the downgrade refuses while any task carries them.

Production runs as ``pgs_app`` without DDL rights, so the grant and RLS policy
live in this migration (the 0023/0029/0031 rule).
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_rule_decision_ledger"
down_revision = "0032_single_active_ai_connection"
branch_labels = None
depends_on = None


TABLE = "rule_decision_ledger"


def _hex_check(column_name):
    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def _configure_postgres_runtime_access() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
    ).scalar()
    if not role_exists:
        return
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO pgs_app" % TABLE)
    op.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % TABLE)
    op.execute("DROP POLICY IF EXISTS pgs_app_dml ON %s" % TABLE)
    op.execute(
        "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
        "USING (true) WITH CHECK (true)" % TABLE
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("scope_key", sa.String(length=200), nullable=False),
        sa.Column("decision_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_code", sa.String(length=100), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _hex_check("decision_identity_hash"),
            name="ck_rule_decision_ledger_identity_hash",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0",
            name="ck_rule_decision_ledger_nonnegative_tokens",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key",
            "decision_identity_hash",
            name="uq_rule_decision_ledger_scope_identity",
        ),
    )
    op.create_index("ix_rule_decision_ledger_expires_at", TABLE, ["expires_at"])
    op.create_index(
        "ix_rule_decision_ledger_organization_id", TABLE, ["organization_id"]
    )
    with op.batch_alter_table("rule_scoring_tasks") as batch:
        batch.add_column(
            sa.Column(
                "decision_reused",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("group_call_id", sa.String(length=64), nullable=True))
    _configure_postgres_runtime_access()


def downgrade() -> None:
    connection = op.get_bind()
    reused = connection.execute(
        sa.text(
            "SELECT count(*) FROM rule_scoring_tasks "
            "WHERE decision_reused = :value OR group_call_id IS NOT NULL"
        ),
        {"value": True},
    ).scalar_one()
    if reused:
        raise RuntimeError(
            "0033 downgrade would erase the reuse audit flag of %d rule task(s); "
            "archive them before downgrading" % reused
        )
    with op.batch_alter_table("rule_scoring_tasks") as batch:
        batch.drop_column("group_call_id")
        batch.drop_column("decision_reused")
    op.drop_index("ix_rule_decision_ledger_organization_id", table_name=TABLE)
    op.drop_index("ix_rule_decision_ledger_expires_at", table_name=TABLE)
    op.drop_table(TABLE)
