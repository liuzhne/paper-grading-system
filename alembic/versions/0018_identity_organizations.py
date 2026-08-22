"""Add multi-user identity, organizations, server sessions, and audit entities.

Revision ID: 0018_identity_organizations
Revises: 0017_batch_scoring_jobs
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_identity_organizations"
down_revision = "0017_batch_scoring_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("platform_role", sa.String(length=50), nullable=False, server_default="user"))
    op.create_index("ix_users_email_unique", "users", ["email"], unique=True)

    op.create_table(
        "organizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_organizations_name"),
    )
    op.create_table(
        "organization_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_organization_membership"),
    )
    op.create_index("ix_organization_members_organization", "organization_members", ["organization_id"])
    op.create_index("ix_organization_members_user", "organization_members", ["user_id"])
    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_organization_invitations_token"),
    )
    op.create_index("ix_organization_invitations_organization", "organization_invitations", ["organization_id"])
    op.create_index("ix_organization_invitations_email", "organization_invitations", ["email"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"])
    for table_name in ("email_verification_tokens", "password_reset_tokens"):
        op.create_table(
            table_name,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("token", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token", name="uq_%s_token" % table_name),
        )
        op.create_index("ix_%s_user" % table_name, table_name, ["user_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_actor", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_organization", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])


def downgrade():
    connection = op.get_bind()
    protected_tables = (
        "audit_logs",
        "password_reset_tokens",
        "email_verification_tokens",
        "auth_sessions",
        "organization_members",
        "organization_invitations",
        "organizations",
    )
    if any(connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % table)).scalar() for table in protected_tables):
        raise RuntimeError("0018_identity_organizations downgrade would lose identity, session, or audit state")
    for index_name in ("ix_audit_logs_event_type", "ix_audit_logs_organization", "ix_audit_logs_actor"):
        op.drop_index(index_name, table_name="audit_logs")
    op.drop_table("audit_logs")
    for table_name in ("password_reset_tokens", "email_verification_tokens"):
        op.drop_index("ix_%s_user" % table_name, table_name=table_name)
        op.drop_table(table_name)
    op.drop_index("ix_auth_sessions_user", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_organization_members_user", table_name="organization_members")
    op.drop_index("ix_organization_members_organization", table_name="organization_members")
    op.drop_index("ix_organization_invitations_email", table_name="organization_invitations")
    op.drop_index("ix_organization_invitations_organization", table_name="organization_invitations")
    op.drop_table("organization_invitations")
    op.drop_table("organization_members")
    op.drop_table("organizations")
    op.drop_index("ix_users_email_unique", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("platform_role")
        batch.drop_column("email_verified_at")
        batch.drop_column("email")
