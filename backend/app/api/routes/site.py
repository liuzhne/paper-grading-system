"""官网申请：公开提交 + 平台管理员跟进（docs/官网申请落库改造方案.md）。

公开提交不登录、不带组织上下文，所以本路由组挂在 `guarded` 之外；三个管理接口
逐个经 `current_principal` + `require_platform_admin`。
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_platform_admin
from backend.app.core.config import settings
from backend.app.db.models import SiteLead
from backend.app.db.models import utcnow
from backend.app.db.session import get_db
from backend.app.schemas.site import SiteLeadKind
from backend.app.schemas.site import SiteLeadPage
from backend.app.schemas.site import SiteLeadRead
from backend.app.schemas.site import SiteLeadReceived
from backend.app.schemas.site import SiteLeadStatus
from backend.app.schemas.site import SiteLeadStatusUpdate
from backend.app.schemas.site import SiteLeadSubmit
from backend.app.services import site_leads
from backend.app.services.auth import audit
from backend.app.services.auth import auth_active


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/site", tags=["site"])

ORGANIZATION_MAX = 120
NOTE_MAX = 500


def _disabled() -> JSONResponse:
    # 开关关着时接口当作不存在：Vercel 与内网部署不收申请。
    return JSONResponse(status_code=404, content={"detail": {"code": "site_leads_disabled"}})


def _invalid(fields: dict[str, str]) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_fields", "fields": fields})


@router.post("/leads", status_code=202, response_model=SiteLeadReceived)
def submit_site_lead(payload: SiteLeadSubmit, request: Request, db: Session = Depends(get_db)):
    if not settings.SITE_LEADS_ENABLED:
        return _disabled()
    if site_leads.clean_text(payload.website):
        # 脚本往往把每个输入框都填上；对它返回同样的 202，不告诉它被识破了。
        logger.info("site_lead_honeypot kind=%s", payload.kind)
        return SiteLeadReceived()

    email = site_leads.normalize_email(payload.email)
    organization_name = site_leads.clean_text(payload.organization_name)
    note = site_leads.clean_text(payload.note)
    fields: dict[str, str] = {}
    if not site_leads.valid_email(email):
        fields["email"] = "请填写有效的邮箱"
    if payload.kind == "deploy_assessment":
        if not organization_name:
            fields["organization_name"] = "请填写学校与院系"
        elif len(organization_name) > ORGANIZATION_MAX:
            fields["organization_name"] = "学校与院系最多 %s 个字" % ORGANIZATION_MAX
        if len(note) > NOTE_MAX:
            fields["note"] = "备注最多 %s 个字" % NOTE_MAX
    else:
        # 试用申请只收邮箱；其余字段即使带来也不存。
        organization_name = ""
        note = ""
    if fields:
        raise _invalid(fields)

    client_ip = request.client.host if request.client else None
    try:
        result = site_leads.submit_lead(
            db,
            kind=payload.kind,
            email=email,
            organization_name=organization_name,
            note=note,
            ip=client_ip,
        )
    except site_leads.SiteLeadRejected as exc:
        db.rollback()
        logger.info("site_lead_rejected kind=%s reason=%s", payload.kind, exc.code)
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
            content={"detail": {"code": exc.code, "message": "提交太频繁，请稍后再试"}},
        )
    db.commit()
    # 不记邮箱：日志会被转发、留存，范围比数据库大。
    logger.info(
        "site_lead_received id=%s kind=%s merged=%s", result.lead.id, payload.kind, result.merged
    )
    return SiteLeadReceived()


@router.get("/leads", response_model=SiteLeadPage)
def list_site_leads(
    status: SiteLeadStatus | None = None,
    kind: SiteLeadKind | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    require_platform_admin(principal)
    items, total, new_count = site_leads.list_leads(
        db, status=status, kind=kind, limit=limit, offset=offset
    )
    return SiteLeadPage(
        items=[SiteLeadRead.model_validate(item) for item in items],
        total=total,
        new_count=new_count,
    )


def _lead_or_404(db: Session, lead_id: str) -> SiteLead:
    lead = db.get(SiteLead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="申请不存在或已删除")
    return lead


@router.patch("/leads/{lead_id}", response_model=SiteLeadRead)
def update_site_lead(
    lead_id: str,
    payload: SiteLeadStatusUpdate,
    principal: CurrentPrincipal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    require_platform_admin(principal)
    lead = _lead_or_404(db, lead_id)
    previous = lead.status
    lead.status = payload.status
    lead.handled_at = utcnow()
    # 开发模式的占位用户不在 users 表里，外键会挡住；只在真实会话下记操作人。
    lead.handled_by = principal.user_id if auth_active() else None
    audit(
        db,
        "site_lead.status_changed",
        actor_id=principal.user_id if auth_active() else None,
        metadata={"lead_id": lead.id, "kind": lead.kind, "from": previous, "to": payload.status},
    )
    db.commit()
    db.refresh(lead)
    return SiteLeadRead.model_validate(lead)


@router.delete("/leads/{lead_id}", status_code=204)
def delete_site_lead(
    lead_id: str,
    principal: CurrentPrincipal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    require_platform_admin(principal)
    lead = _lead_or_404(db, lead_id)
    kind = lead.kind
    db.delete(lead)
    audit(
        db,
        "site_lead.deleted",
        actor_id=principal.user_id if auth_active() else None,
        metadata={"lead_id": lead_id, "kind": kind},
    )
    db.commit()
