"""Store applications submitted from the product site (官网试用与部署评估申请).

Revision ID: 0039_site_leads
Revises: 0038_assistant_conversations
Create Date: 2026-10-18

One new table, so the 0023 rule applies: grant ``pgs_app`` and enable RLS in
the migration itself.  Production runs as ``pgs_app`` without DDL rights;
forgetting the grant does not fail the migration, it makes the public form
fail with "permission denied" right after a successful deploy.

The table is platform-level (applications arrive before any organization
exists).  It stores only what is needed to reply: kind, email, school and
department, an optional note, the page it came from and a status.  The raw
client IP is never stored; ``ip_hash`` is an HMAC used for rate limiting and
is deleted together with the row when the retention period ends
(docs/官网申请落库改造方案.md).

The downgrade refuses while the table has rows: these are visitors' requests
that have not necessarily been answered, not a cache.
"""

from alembic import op
import sqlalchemy as sa


revision = "0039_site_leads"
down_revision = "0038_assistant_conversations"
branch_labels = None
depends_on = None


TABLE = "site_leads"


def _configure_postgres_runtime_access():
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
    op.execute(
        "CREATE POLICY pgs_app_dml ON %s FOR ALL TO pgs_app "
        "USING (true) WITH CHECK (true)" % TABLE
    )


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("organization_name", sa.String(length=120), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("source_path", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("submit_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_submitted_at", sa.DateTime(), nullable=False),
        sa.Column("handled_by", sa.String(length=36), nullable=True),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('trial', 'deploy_assessment')", name="ck_site_leads_kind"
        ),
        sa.CheckConstraint(
            "status IN ('new', 'contacted', 'closed', 'spam')", name="ck_site_leads_status"
        ),
        sa.CheckConstraint("submit_count >= 1", name="ck_site_leads_submit_count"),
        sa.ForeignKeyConstraint(["handled_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_site_leads_kind_email", TABLE, ["kind", "email"])
    op.create_index("ix_site_leads_ip_hash_submitted", TABLE, ["ip_hash", "last_submitted_at"])
    op.create_index("ix_site_leads_status_submitted", TABLE, ["status", "last_submitted_at"])
    _configure_postgres_runtime_access()


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % TABLE)).scalar():
        raise RuntimeError(
            "0039_site_leads downgrade would drop site applications in %s; "
            "delete them explicitly before downgrading" % TABLE
        )
    op.drop_index("ix_site_leads_status_submitted", table_name=TABLE)
    op.drop_index("ix_site_leads_ip_hash_submitted", table_name=TABLE)
    op.drop_index("ix_site_leads_kind_email", table_name=TABLE)
    op.drop_table(TABLE)
