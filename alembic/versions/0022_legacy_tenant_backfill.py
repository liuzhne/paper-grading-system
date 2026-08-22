"""Backfill pre-tenant data into the immutable default organization.

Revision ID: 0022_legacy_tenant_backfill
Revises: 0021_private_ai_connections
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0022_legacy_tenant_backfill"
down_revision = "0021_private_ai_connections"
branch_labels = None
depends_on = None


DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_ORGANIZATION_NAME = "Default Organization"


def _exists(connection, statement, **params):
    return connection.execute(sa.text(statement), params).scalar() is not None


def upgrade():
    """Make pre-0022 rows visible under one safe, auditable tenant boundary."""
    connection = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if not _exists(connection, "SELECT 1 FROM users WHERE id = :id", id=DEFAULT_USER_ID):
        op.bulk_insert(
            sa.table(
                "users",
                sa.column("id", sa.String),
                sa.column("username", sa.String),
                sa.column("display_name", sa.String),
                sa.column("role", sa.String),
                sa.column("password_hash", sa.String),
                sa.column("email", sa.String),
                sa.column("email_verified_at", sa.DateTime),
                sa.column("platform_role", sa.String),
                sa.column("created_at", sa.DateTime),
                sa.column("updated_at", sa.DateTime),
            ),
            [
                {
                    "id": DEFAULT_USER_ID,
                    "username": "dev-user",
                    "display_name": "Bootstrap Admin",
                    "role": "platform_admin",
                    "password_hash": None,
                    "email": None,
                    "email_verified_at": now,
                    "platform_role": "platform_admin",
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
    else:
        connection.execute(
            sa.text(
                "UPDATE users SET platform_role = 'platform_admin', "
                "email_verified_at = COALESCE(email_verified_at, :now), "
                "role = 'platform_admin', updated_at = :now WHERE id = :id"
            ),
            {"id": DEFAULT_USER_ID, "now": now},
        )

    default_organization_id = connection.execute(
        sa.text("SELECT id FROM organizations WHERE name = :name"),
        {"name": DEFAULT_ORGANIZATION_NAME},
    ).scalar()
    if default_organization_id is None:
        default_organization_id = DEFAULT_ORGANIZATION_ID
        op.bulk_insert(
            sa.table(
                "organizations",
                sa.column("id", sa.String),
                sa.column("name", sa.String),
                sa.column("created_by", sa.String),
                sa.column("created_at", sa.DateTime),
                sa.column("updated_at", sa.DateTime),
            ),
            [{"id": default_organization_id, "name": DEFAULT_ORGANIZATION_NAME, "created_by": DEFAULT_USER_ID, "created_at": now, "updated_at": now}],
        )

    user_ids = connection.execute(sa.text("SELECT id FROM users")).scalars().all()
    membership_table = sa.table(
        "organization_members",
        sa.column("id", sa.String),
        sa.column("organization_id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("role", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    for user_id in user_ids:
        if not _exists(
            connection,
            "SELECT 1 FROM organization_members WHERE organization_id = :organization_id AND user_id = :user_id",
            organization_id=default_organization_id,
            user_id=user_id,
        ):
            # Existing single-tenant users retain the ability to operate their
            # historical workloads; only the legacy bootstrap user is org admin.
            op.bulk_insert(
                membership_table,
                [{
                    "id": "%s-%s" % (default_organization_id[:18], user_id[-17:]),
                    "organization_id": default_organization_id,
                    "user_id": user_id,
                    "role": "org_admin" if user_id == DEFAULT_USER_ID else "teacher",
                    "created_at": now,
                }],
            )

    for table_name in (
        "rubrics",
        "grading_batches",
        "papers",
        "scoring_runs",
        "evaluation_batches",
        "submissions",
        "rubric_versions",
    ):
        connection.execute(
            sa.text("UPDATE %s SET organization_id = :organization_id WHERE organization_id IS NULL" % table_name),
            {"organization_id": default_organization_id},
        )

    # owner_id already existed on legacy thesis entities.  Make old null-owner
    # rows explicitly attributable without altering model/provider snapshots.
    for table_name in ("rubrics", "grading_batches", "papers", "scoring_runs"):
        connection.execute(
            sa.text("UPDATE %s SET owner_id = :owner_id WHERE owner_id IS NULL" % table_name),
            {"owner_id": DEFAULT_USER_ID},
        )


def downgrade():
    """Allow empty CI replay, but never remove real tenant authorization data."""
    connection = op.get_bind()
    scoped_tables = (
        "rubrics",
        "grading_batches",
        "papers",
        "scoring_runs",
        "evaluation_batches",
        "submissions",
        "rubric_versions",
    )
    if any(
        connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM %s WHERE organization_id IS NOT NULL)" % table)
        ).scalar()
        for table in scoped_tables
    ):
        raise RuntimeError("0022_legacy_tenant_backfill downgrade would lose tenant ownership and authorization history")

    member_ids = connection.execute(
        sa.text("SELECT user_id FROM organization_members WHERE organization_id = :organization_id"),
        {"organization_id": DEFAULT_ORGANIZATION_ID},
    ).scalars().all()
    if any(user_id != DEFAULT_USER_ID for user_id in member_ids):
        raise RuntimeError("0022_legacy_tenant_backfill downgrade would lose tenant ownership and authorization history")
    for table in (
        "audit_logs",
        "password_reset_tokens",
        "email_verification_tokens",
        "auth_sessions",
        "organization_invitations",
    ):
        if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % table)).scalar():
            raise RuntimeError("0022_legacy_tenant_backfill downgrade would lose tenant ownership and authorization history")

    connection.execute(
        sa.text("DELETE FROM organization_members WHERE organization_id = :organization_id"),
        {"organization_id": DEFAULT_ORGANIZATION_ID},
    )
    connection.execute(
        sa.text("DELETE FROM organizations WHERE id = :organization_id"),
        {"organization_id": DEFAULT_ORGANIZATION_ID},
    )
