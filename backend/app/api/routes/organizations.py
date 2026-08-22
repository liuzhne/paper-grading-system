import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal, current_principal
from backend.app.db.models import Organization
from backend.app.db.models import OrganizationMember
from backend.app.db.models import OrganizationInvitation
from backend.app.db.models import User
from backend.app.db.models import utcnow
from backend.app.db.session import get_db
from backend.app.services.auth import audit

router = APIRouter(prefix="/organizations", tags=["organizations"])


class MemberCreate(BaseModel):
    username: str | None = None
    email: str | None = None
    role: str = "member"


class MemberRoleUpdate(BaseModel):
    role: str


@router.get("")
def list_organizations(principal: CurrentPrincipal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == principal.user_id)
        .order_by(Organization.name)
    ).all()
    return [{"id": organization.id, "name": organization.name, "role": role} for organization, role in rows]


@router.get("/{organization_id}/members")
def list_members(
    organization_id: str,
    principal: CurrentPrincipal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    if principal.platform_role != "platform_admin" and (
        principal.organization_id != organization_id or principal.organization_role != "org_admin"
    ):
        # Do not reveal membership or organisation existence to unauthorized users.
        raise HTTPException(status_code=404, detail="organization not found")
    rows = db.execute(
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(User.username)
    ).all()
    return [
        {
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": membership.role,
        }
        for membership, user in rows
    ]


@router.post("/{organization_id}/members", status_code=201)
def add_member(
    organization_id: str,
    payload: MemberCreate,
    principal: CurrentPrincipal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    if principal.platform_role != "platform_admin" and (
        principal.organization_id != organization_id or principal.organization_role != "org_admin"
    ):
        raise HTTPException(status_code=403, detail="无权管理组织成员")
    if payload.role not in {"org_admin", "teacher", "member"}:
        raise HTTPException(status_code=422, detail="无效的组织角色")
    if bool(payload.username) == bool(payload.email):
        raise HTTPException(status_code=422, detail="请提供 username 或 email 其中之一")
    if payload.email:
        invitation = OrganizationInvitation(
            organization_id=organization_id,
            email=payload.email.casefold(),
            role=payload.role,
            token=secrets.token_urlsafe(32),
            expires_at=utcnow() + timedelta(seconds=604800),
            created_by=principal.user_id,
        )
        db.add(invitation)
        audit(db, "organization.member_invited", actor_id=principal.user_id, organization_id=organization_id, metadata={"email": invitation.email, "role": payload.role})
        db.commit()
        return {"organization_id": organization_id, "email": invitation.email, "role": invitation.role, "status": "invited"}
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    membership = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if membership is None:
        membership = OrganizationMember(organization_id=organization_id, user_id=user.id, role=payload.role)
        db.add(membership)
    else:
        membership.role = payload.role
    audit(db, "organization.member_upserted", actor_id=principal.user_id, organization_id=organization_id, metadata={"member_id": user.id, "role": payload.role})
    db.commit()
    return {"user_id": user.id, "organization_id": organization_id, "role": membership.role}


@router.patch("/{organization_id}/members/{user_id}")
def update_member_role(
    organization_id: str,
    user_id: str,
    payload: MemberRoleUpdate,
    principal: CurrentPrincipal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    if principal.platform_role != "platform_admin" and (
        principal.organization_id != organization_id or principal.organization_role != "org_admin"
    ):
        raise HTTPException(status_code=404, detail="organization not found")
    if payload.role not in {"org_admin", "teacher", "member"}:
        raise HTTPException(status_code=422, detail="无效的组织角色")
    membership = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="organization member not found")
    membership.role = payload.role
    audit(
        db,
        "organization.member_role_updated",
        actor_id=principal.user_id,
        organization_id=organization_id,
        metadata={"member_id": user_id, "role": payload.role},
    )
    db.commit()
    return {"user_id": user_id, "organization_id": organization_id, "role": membership.role}
