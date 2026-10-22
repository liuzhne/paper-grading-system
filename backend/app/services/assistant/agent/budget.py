"""Agent 回合的上限（Agent 化方案 §10、§15）。

每回合：工具调用最多 8 次（其中搜索最多 2 次）、60 秒；每次模型调用截止 20 秒、只试 1 次、
不原地等 429。每人每天最多 `ASSISTANT_AGENT_DAILY_TURNS` 个 Agent 回合，超出后当天只用规则模式。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import AssistantConversation
from backend.app.db.models import AssistantMessage
from backend.app.db.models import utcnow

MAX_TOOL_CALLS = 8
MAX_SEARCHES = 2
TURN_SECONDS = 60
STEP_SECONDS = 20
STEP_MAX_TOKENS = 1200
#: Agent 回合写在助手消息上的意图，按它统计每日回合数。
AGENT_INTENT = "agent"


def day_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def turns_today(db: Session, user_id: str, now: datetime | None = None) -> int:
    start = day_start(now)
    return db.scalar(
        select(func.count(AssistantMessage.id))
        .join(AssistantConversation, AssistantConversation.id == AssistantMessage.conversation_id)
        .where(
            AssistantConversation.owner_id == user_id,
            AssistantMessage.role == "assistant",
            AssistantMessage.intent == AGENT_INTENT,
            AssistantMessage.created_at >= start,
            AssistantMessage.created_at < start + timedelta(days=1),
        )
    ) or 0


def daily_budget_left(db: Session, user_id: str) -> bool:
    return turns_today(db, user_id) < max(0, int(settings.ASSISTANT_AGENT_DAILY_TURNS))


__all__ = ["AGENT_INTENT", "MAX_SEARCHES", "MAX_TOOL_CALLS", "STEP_MAX_TOKENS", "STEP_SECONDS", "TURN_SECONDS",
           "daily_budget_left", "turns_today"]
