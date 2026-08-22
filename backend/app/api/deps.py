"""FastAPI authentication and organization-context dependencies."""

from dataclasses import dataclass

from fastapi import Cookie
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import OrganizationMember
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.services.auth import active_session
from backend.app.services.auth import auth_active


@dataclass(frozen=True)
class CurrentPrincipal:
    user_id: str
    organization_id: str | None
    organization_role: str | None
    platform_role: str


def _session_token(cookie_token: str | None) -> str | None:
    return cookie_token


def current_principal(
    db: Session = Depends(get_db),
    pgs_session: str | None = Cookie(default=None, alias="pgs_session"),
    requested_organization_id: str | None = Header(default=None, alias="X-Organization-ID"),
) -> CurrentPrincipal:
    if not auth_active():
        return CurrentPrincipal(settings.DEFAULT_DEV_USER_ID, None, None, "developer")
    session = active_session(db, _session_token(pgs_session))
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话失效")
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话失效")
    selected_organization_id = requested_organization_id or session.organization_id
    membership = None
    if selected_organization_id:
        membership = db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == selected_organization_id,
                OrganizationMember.user_id == user.id,
            )
        )
        if membership is None and user.platform_role != "platform_admin":
            raise HTTPException(status_code=403, detail="无权访问该组织")
    return CurrentPrincipal(
        user_id=user.id,
        organization_id=selected_organization_id,
        organization_role=membership.role if membership else None,
        platform_role=user.platform_role,
    )


def enforce_auth(principal: CurrentPrincipal = Depends(current_principal)):
    return principal


def current_user_id(principal: CurrentPrincipal = Depends(current_principal)) -> str:
    return principal.user_id


def require_organization_role(
    principal: CurrentPrincipal,
    *roles: str,
) -> None:
    """Allow privileged platform users and the specified organization roles."""
    if principal.platform_role == "platform_admin" or principal.organization_id is None:
        return
    if principal.organization_role not in roles:
        raise HTTPException(status_code=403, detail="当前组织角色无权执行此操作")
