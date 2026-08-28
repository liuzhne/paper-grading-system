"""账户密码、可撤销服务端会话和 Bootstrap Admin 支撑。"""

import hashlib
import secrets
from datetime import timedelta

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import AuditLog
from backend.app.db.models import AuthSession
from backend.app.db.models import Organization
from backend.app.db.models import OrganizationMember
from backend.app.db.models import User
from backend.app.db.models import utcnow


def auth_active() -> bool:
    return bool(settings.AUTH_ENABLED and settings.AUTH_PASSWORD)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def password_matches(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User, organization_id: str | None) -> str:
    token = secrets.token_urlsafe(48)
    db.add(
        AuthSession(
            token_hash=token_digest(token),
            user_id=user.id,
            organization_id=organization_id,
            expires_at=utcnow() + timedelta(seconds=settings.AUTH_TOKEN_TTL_SECONDS),
        )
    )
    return token


def active_session(db: Session, token: str | None) -> AuthSession | None:
    if not token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_digest(token)))
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        return None
    return session


def revoke_session(db: Session, token: str | None) -> None:
    session = active_session(db, token)
    if session is not None:
        session.revoked_at = utcnow()


def audit(db: Session, event_type: str, *, actor_id: str | None = None, organization_id: str | None = None, metadata=None):
    db.add(
        AuditLog(
            actor_id=actor_id,
            organization_id=organization_id,
            event_type=event_type,
            event_metadata=metadata or {},
        )
    )


def ensure_bootstrap_admin(db: Session) -> User:
    """Only create the deployment bootstrap account once, on first real login."""

    username = settings.AUTH_USERNAME.strip()
    user = db.scalar(select(User).where(User.username == username))
    if user is not None:
        return user

    user = User(
        username=username,
        display_name="Bootstrap Admin",
        email="bootstrap-admin@local.invalid",
        password_hash=hash_password(settings.AUTH_PASSWORD or ""),
        platform_role="platform_admin",
        role="platform_admin",
        email_verified_at=utcnow(),
    )
    db.add(user)
    db.flush()
    audit(db, "auth.bootstrap_admin_created", actor_id=user.id)

    organization = db.scalar(select(Organization).where(Organization.name == settings.DEFAULT_ORGANIZATION_NAME))
    if organization is None:
        organization = Organization(name=settings.DEFAULT_ORGANIZATION_NAME, created_by=user.id)
        db.add(organization)
        db.flush()
    membership = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == user.id,
        )
    )
    if membership is None:
        db.add(OrganizationMember(organization_id=organization.id, user_id=user.id, role="org_admin"))
    return user


def primary_organization_id(db: Session, user_id: str) -> str | None:
    return db.scalar(
        select(OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(OrganizationMember.created_at)
    )
