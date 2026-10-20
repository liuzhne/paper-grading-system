"""官网试用与部署评估申请（docs/官网申请落库改造方案.md）。

公开提交只做四件事：校验、防滥用、去重、写入。管理员接口只给平台管理员。
日志与审计都不记邮箱；原始 IP 不落库，`ip_hash` 是 HMAC 摘要，只用于限流。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import hashlib
import hmac
import logging
import re

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import SiteLead
from backend.app.db.models import utcnow


logger = logging.getLogger(__name__)

KINDS = ("trial", "deploy_assessment")
STATUSES = ("new", "contacted", "closed", "spam")
#: 申请来自哪张页面；访客不能自己填，按类型确定。
SOURCE_PATHS = {"trial": "/", "deploy_assessment": "/about"}
#: 同一类型、同一邮箱在这段时间内再次提交时合并进原记录，不新增一行。
DEDUPE_WINDOW = timedelta(days=30)
#: 仍在跟进中的申请才合并；已结束或标为垃圾的，再来就是一条新申请。
DEDUPE_STATUSES = ("new", "contacted")
PURGE_STATE = "site_leads_purge"

# 不追求 RFC 5322 全集：本地部分与域名都不能有空白，域名至少一个点。
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SiteLeadRejected(Exception):
    """限流或日上限：提交被拒，但不是访客填错了。"""

    def __init__(self, code: str, retry_after: int):
        super().__init__(code)
        self.code = code
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class SubmittedLead:
    lead: SiteLead
    merged: bool


def clean_text(value: str | None) -> str:
    """去掉控制字符与首尾空白；换行保留给备注。"""

    return _CONTROL.sub("", value or "").strip()


def normalize_email(value: str | None) -> str:
    return clean_text(value).casefold()


def valid_email(value: str) -> bool:
    return len(value) <= 254 and bool(_EMAIL.match(value))


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    return hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _retry_after(oldest: datetime | None, window: timedelta, now: datetime) -> int:
    if oldest is None:
        return int(window.total_seconds())
    return int((oldest + window - now).total_seconds()) + 1


def _enforce_ip_limits(db: Session, ip_hash: str | None, now: datetime) -> None:
    if ip_hash is None:
        return
    for window, limit, code in (
        (timedelta(hours=1), settings.SITE_LEADS_PER_IP_HOURLY, "per_ip_hourly"),
        (timedelta(days=1), settings.SITE_LEADS_PER_IP_DAILY, "per_ip_daily"),
    ):
        since = now - window
        count, oldest = db.execute(
            select(func.count(SiteLead.id), func.min(SiteLead.last_submitted_at)).where(
                SiteLead.ip_hash == ip_hash, SiteLead.last_submitted_at >= since
            )
        ).one()
        if count >= max(1, limit):
            raise SiteLeadRejected(code, _retry_after(oldest, window, now))


def _enforce_daily_limit(db: Session, now: datetime) -> None:
    window = timedelta(days=1)
    count, oldest = db.execute(
        select(func.count(SiteLead.id), func.min(SiteLead.created_at)).where(
            SiteLead.created_at >= now - window
        )
    ).one()
    if count >= max(1, settings.SITE_LEADS_DAILY_LIMIT):
        # 全站被刷时库的增长有上界；记一条 WARNING，管理员能从日志看到。
        logger.warning("site_lead_daily_limit_reached count=%s", count)
        raise SiteLeadRejected("daily_limit", _retry_after(oldest, window, now))


def submit_lead(
    db: Session,
    *,
    kind: str,
    email: str,
    organization_name: str | None,
    note: str | None,
    ip: str | None,
    now: datetime | None = None,
) -> SubmittedLead:
    """写入或合并一条申请。调用方负责校验字段并提交事务。"""

    now = now or utcnow()
    ip_hash = hash_ip(ip)
    _enforce_ip_limits(db, ip_hash, now)

    existing = db.scalar(
        select(SiteLead)
        .where(
            SiteLead.kind == kind,
            SiteLead.email == email,
            SiteLead.status.in_(DEDUPE_STATUSES),
            SiteLead.last_submitted_at >= now - DEDUPE_WINDOW,
        )
        .order_by(SiteLead.last_submitted_at.desc())
        .limit(1)
    )
    if existing is not None:
        existing.submit_count += 1
        existing.last_submitted_at = now
        existing.ip_hash = ip_hash
        if organization_name:
            existing.organization_name = organization_name
        if note:
            existing.note = note
        return SubmittedLead(existing, merged=True)

    _enforce_daily_limit(db, now)
    lead = SiteLead(
        kind=kind,
        email=email,
        organization_name=organization_name or None,
        note=note or None,
        source_path=SOURCE_PATHS[kind],
        status="new",
        submit_count=1,
        ip_hash=ip_hash,
        created_at=now,
        last_submitted_at=now,
    )
    db.add(lead)
    return SubmittedLead(lead, merged=False)


def list_leads(
    db: Session,
    *,
    status: str | None,
    kind: str | None,
    limit: int,
    offset: int,
) -> tuple[list[SiteLead], int, int]:
    filters = []
    if status:
        filters.append(SiteLead.status == status)
    if kind:
        filters.append(SiteLead.kind == kind)
    total = db.scalar(select(func.count(SiteLead.id)).where(*filters)) or 0
    items = db.scalars(
        select(SiteLead)
        .where(*filters)
        .order_by(SiteLead.last_submitted_at.desc(), SiteLead.id)
        .limit(limit)
        .offset(offset)
    ).all()
    new_count = db.scalar(select(func.count(SiteLead.id)).where(SiteLead.status == "new")) or 0
    return list(items), int(total), int(new_count)


def purge_expired(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    cutoff = now - timedelta(days=max(1, settings.SITE_LEADS_RETENTION_DAYS))
    result = db.execute(delete(SiteLead).where(SiteLead.last_submitted_at < cutoff))
    return int(result.rowcount or 0)


def purge_expired_if_due(session_factory, now: datetime | None = None) -> int | None:
    """worker 巡检时调用：每天最多删一次过期申请；没到时间返回 None。"""

    from backend.app.services.work_queue.state import try_acquire

    now = now or utcnow()
    if not try_acquire(session_factory, PURGE_STATE, now, min_interval_seconds=86400):
        return None
    with session_factory() as session:
        removed = purge_expired(session, now)
        session.commit()
    logger.info("site_leads_purged removed=%s", removed)
    return removed


__all__ = [
    "DEDUPE_WINDOW",
    "KINDS",
    "STATUSES",
    "SiteLeadRejected",
    "SubmittedLead",
    "clean_text",
    "hash_ip",
    "list_leads",
    "normalize_email",
    "purge_expired",
    "purge_expired_if_due",
    "submit_lead",
    "valid_email",
]
