"""账户注册、邮箱验证、可撤销 Cookie 会话与密码重置端点。"""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal, current_principal
from backend.app.core.config import settings
from backend.app.db.models import EmailVerificationToken, Organization, OrganizationInvitation, OrganizationMember, PasswordResetToken, User, utcnow
from backend.app.db.session import get_db
from backend.app.services.auth import active_session, audit, auth_active, create_session, ensure_bootstrap_admin, hash_password, password_matches, primary_organization_id, revoke_session

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class RegistrationRequest(BaseModel):
    username: str
    email: str
    display_name: str
    password: str
    invitation_token: str | None = None


class TokenRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirm(TokenRequest):
    password: str


class OrganizationContextRequest(BaseModel):
    organization_id: str


def _cookie(response: Response, token: str):
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        max_age=settings.AUTH_TOKEN_TTL_SECONDS,
        path="/",
    )


def _new_verification_token(db: Session, user_id: str):
    db.add(
        EmailVerificationToken(
            token=secrets.token_urlsafe(32),
            user_id=user_id,
            expires_at=utcnow() + timedelta(seconds=settings.EMAIL_VERIFICATION_TOKEN_TTL_SECONDS),
        )
    )


@router.get("/status")
def auth_status():
    return {"auth_required": auth_active(), "registration_mode": settings.REGISTRATION_MODE}


@router.post("/register", status_code=201)
def register(payload: RegistrationRequest, db: Session = Depends(get_db)):
    invitation = None
    if payload.invitation_token:
        invitation = db.scalar(select(OrganizationInvitation).where(OrganizationInvitation.token == payload.invitation_token))
        if (
            invitation is None
            or invitation.accepted_at is not None
            or invitation.expires_at <= utcnow()
            or invitation.email != payload.email.casefold()
        ):
            raise HTTPException(status_code=400, detail="邀请链接无效或已过期")
    if settings.REGISTRATION_MODE != "public" and invitation is None:
        raise HTTPException(status_code=403, detail="当前部署仅允许邀请注册")
    if len(payload.password) < 12:
        raise HTTPException(status_code=422, detail="密码至少需要 12 个字符")
    if db.scalar(select(User).where(User.username == payload.username)) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    if db.scalar(select(User).where(User.email == payload.email.casefold())) is not None:
        raise HTTPException(status_code=409, detail="邮箱已存在")
    user = User(
        username=payload.username,
        email=payload.email.casefold(),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        platform_role="user",
    )
    db.add(user)
    db.flush()
    if invitation is None:
        organization = Organization(name="%s workspace" % payload.username, created_by=user.id)
        db.add(organization)
        db.flush()
        member_role = "member"
    else:
        organization = db.get(Organization, invitation.organization_id)
        member_role = invitation.role
        invitation.accepted_at = utcnow()
    db.add(OrganizationMember(organization_id=organization.id, user_id=user.id, role=member_role))
    _new_verification_token(db, user.id)
    audit(db, "auth.registered", actor_id=user.id, organization_id=organization.id)
    db.commit()
    return {"id": user.id, "email_verification_required": True}


@router.post("/verify-email", status_code=204)
def verify_email(payload: TokenRequest, db: Session = Depends(get_db)):
    token = db.scalar(select(EmailVerificationToken).where(EmailVerificationToken.token == payload.token))
    if token is None or token.consumed_at is not None or token.expires_at <= utcnow():
        raise HTTPException(status_code=400, detail="验证链接无效或已过期")
    user = db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="验证链接无效或已过期")
    token.consumed_at = utcnow()
    user.email_verified_at = utcnow()
    audit(db, "auth.email_verified", actor_id=user.id)
    db.commit()
    return Response(status_code=204)


@router.post("/login", status_code=204)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    if not auth_active():
        return Response(status_code=204)
    if payload.username == settings.AUTH_USERNAME:
        ensure_bootstrap_admin(db)
        db.flush()
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not password_matches(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.email_verified_at is None and user.platform_role != "platform_admin":
        raise HTTPException(status_code=403, detail="请先完成邮箱验证")
    organization_id = primary_organization_id(db, user.id)
    token = create_session(db, user, organization_id)
    audit(db, "auth.logged_in", actor_id=user.id, organization_id=organization_id)
    db.commit()
    response = Response(status_code=204)
    _cookie(response, token)
    return response


@router.post("/logout", status_code=204)
def logout(db: Session = Depends(get_db), pgs_session: str | None = Cookie(default=None, alias="pgs_session")):
    revoke_session(db, pgs_session)
    db.commit()
    response = Response(status_code=204)
    response.delete_cookie(settings.AUTH_COOKIE_NAME, path="/")
    return response


@router.get("/me")
def me(principal: CurrentPrincipal = Depends(current_principal), db: Session = Depends(get_db)):
    if not auth_active():
        return {"auth_required": False, "user": None, "organization": None}
    user = db.get(User, principal.user_id)
    return {
        "auth_required": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "platform_role": principal.platform_role,
        },
        "organization": {"id": principal.organization_id, "role": principal.organization_role},
    }


@router.post("/organization-context")
def select_organization_context(
    payload: OrganizationContextRequest,
    db: Session = Depends(get_db),
    pgs_session: str | None = Cookie(default=None, alias="pgs_session"),
):
    """Persist the selected organization on the existing HTTP-only session."""
    if not auth_active():
        raise HTTPException(status_code=400, detail="organization context is unavailable when authentication is disabled")
    session = active_session(db, pgs_session)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话失效")
    user = db.get(User, session.user_id)
    membership = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == payload.organization_id,
            OrganizationMember.user_id == session.user_id,
        )
    )
    if membership is None and (user is None or user.platform_role != "platform_admin"):
        # Keep non-member organization identities undiscoverable.
        raise HTTPException(status_code=404, detail="organization not found")
    if db.get(Organization, payload.organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    session.organization_id = payload.organization_id
    audit(
        db,
        "auth.organization_context_selected",
        actor_id=session.user_id,
        organization_id=payload.organization_id,
    )
    db.commit()
    return {
        "organization": {
            "id": payload.organization_id,
            "role": membership.role if membership is not None else None,
        }
    }


@router.post("/password-reset/request", status_code=204)
def request_password_reset(payload: PasswordResetRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.casefold()))
    if user is not None:
        db.add(PasswordResetToken(token=secrets.token_urlsafe(32), user_id=user.id, expires_at=utcnow() + timedelta(seconds=settings.PASSWORD_RESET_TOKEN_TTL_SECONDS)))
        audit(db, "auth.password_reset_requested", actor_id=user.id)
        db.commit()
    return Response(status_code=204)


@router.post("/password-reset/confirm", status_code=204)
def confirm_password_reset(payload: PasswordResetConfirm, db: Session = Depends(get_db)):
    if len(payload.password) < 12:
        raise HTTPException(status_code=422, detail="密码至少需要 12 个字符")
    token = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token == payload.token))
    if token is None or token.consumed_at is not None or token.expires_at <= utcnow():
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    user = db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    user.password_hash = hash_password(payload.password)
    token.consumed_at = utcnow()
    audit(db, "auth.password_reset_completed", actor_id=user.id)
    db.commit()
    return Response(status_code=204)
