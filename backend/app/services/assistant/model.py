"""评分助手用哪个模型（设计方案 §6.1）。

顺序：用户在助手里选过的 → 当前启用的个人连接（BYOK）→ 平台模型（提醒较慢）
→ 没有模型（规则识别照常可用）。助手模型与评分模型无关：评分仍用批次绑定的
连接。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AIConnection
from backend.app.db.models import AssistantPreference
from backend.app.services import platform_llm
from backend.app.services.ai_connections import resolve_connection_runtime
from backend.app.services.auth import auth_active

logger = logging.getLogger(__name__)

PLATFORM_SLOW_NOTICE = "平台模型由所有用户共用，响应会非常慢；建议在账户页配置自己的模型连接。"


@dataclass(frozen=True)
class EffectiveModel:
    source: str  # connection | platform | deployment | none
    connection_id: str | None = None
    label: str = ""
    slow: bool = False

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "connection_id": self.connection_id,
            "label": self.label,
            "slow": self.slow,
            "notice": PLATFORM_SLOW_NOTICE if self.slow else None,
        }


def get_preference(db: Session, *, user_id: str, organization_id: str | None):
    query = select(AssistantPreference).where(AssistantPreference.user_id == user_id)
    if organization_id is None:
        query = query.where(AssistantPreference.organization_id.is_(None))
    else:
        query = query.where(AssistantPreference.organization_id == organization_id)
    return db.scalars(query.order_by(AssistantPreference.created_at)).first()


def active_connections(db: Session, *, user_id: str, organization_id: str | None):
    if organization_id is None:
        return []
    return db.scalars(
        select(AIConnection).where(
            AIConnection.owner_id == user_id,
            AIConnection.organization_id == organization_id,
            AIConnection.status == "active",
            AIConnection.deleted_at.is_(None),
        ).order_by(AIConnection.created_at)
    ).all()


def platform_available(db: Session) -> bool:
    try:
        return platform_llm.get_active_config(db) is not None
    except Exception:  # 迁移窗口里读不到平台配置，按“没有平台模型”处理。
        db.rollback()
        return False


def _connection_label(connection: AIConnection) -> str:
    return "%s · %s" % (connection.name, connection.model_name)


def effective_model(db: Session, *, user_id: str, organization_id: str | None) -> EffectiveModel:
    if not auth_active():
        # 开发模式的模型来自环境变量（D-028），与评分侧的解析顺序一致。
        return EffectiveModel(source="deployment", label="开发模式模型")

    connections = active_connections(db, user_id=user_id, organization_id=organization_id)
    by_id = {connection.id: connection for connection in connections}
    preference = get_preference(db, user_id=user_id, organization_id=organization_id)
    has_platform = platform_available(db)

    if preference is not None:
        if preference.model_source == "connection" and preference.ai_connection_id in by_id:
            connection = by_id[preference.ai_connection_id]
            return EffectiveModel("connection", connection.id, _connection_label(connection))
        if preference.model_source == "platform" and has_platform:
            return EffectiveModel("platform", label="平台模型", slow=True)
        # 选过的连接已停用或删除：按默认顺序回落，而不是让助手失去理解能力。

    if connections:
        connection = connections[0]
        return EffectiveModel("connection", connection.id, _connection_label(connection))
    if has_platform:
        return EffectiveModel("platform", label="平台模型", slow=True)
    return EffectiveModel("none", label="未配置模型")


def build_scorer(db: Session, model: EffectiveModel, *, user_id: str, organization_id: str | None):
    """返回可调用 complete_json 的适配器；没有模型或构造失败时返回 None。"""

    from backend.app.services.llm.factory import get_llm_scorer

    try:
        if model.source == "connection" and organization_id:
            runtime = resolve_connection_runtime(
                db,
                connection_id=model.connection_id,
                owner_id=user_id,
                organization_id=organization_id,
            )
            return get_llm_scorer(runtime)
        if model.source in ("platform", "deployment"):
            return get_llm_scorer(session=db)
    except Exception as exc:
        logger.warning("assistant_model_unavailable source=%s error=%s", model.source, type(exc).__name__)
    return None
