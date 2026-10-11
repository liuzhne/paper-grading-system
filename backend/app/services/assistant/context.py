"""一次运行的上下文：图的节点通过它写消息、推进卡片、更新焦点与工作区。

上下文经 LangGraph 的 `context=` 传入（`runtime.context`），不进状态快照：
里面有数据库会话与当前用户，既不能也不该被序列化。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.app.api.deps import CurrentPrincipal
from backend.app.db.models import AssistantConversation
from backend.app.db.models import AssistantMessage
from backend.app.db.models import utcnow

#: 焦点里只放编号与名称（方案 §9.2）；`workspace` 与 `workspace_rev` 驱动右侧工作区。
FOCUS_KEYS = ("batch_id", "batch_name", "rubric_id", "rubric_name", "paper_id", "job_id")


def new_card_id(prefix: str) -> str:
    return "%s-%s" % (prefix, secrets.token_hex(6))


@dataclass
class RunContext:
    db: Session
    principal: CurrentPrincipal
    conversation: AssistantConversation
    new_message_ids: list[str] = field(default_factory=list)
    updated_message_ids: set[str] = field(default_factory=set)

    @property
    def user_id(self) -> str:
        return self.principal.user_id

    # --- 消息与卡片 ---------------------------------------------------------------

    def say(self, text: str, cards: list[dict] | None = None, *, intent: str | None = None) -> AssistantMessage:
        normalized = []
        for card in cards or []:
            card = dict(card)
            card.setdefault("id", new_card_id(card.get("type", "card")))
            card.setdefault("status", "info")
            normalized.append(card)
        message = AssistantMessage(
            conversation_id=self.conversation.id,
            role="assistant",
            text=text,
            cards=normalized,
            intent=intent,
        )
        self.db.add(message)
        self.conversation.updated_at = utcnow()
        self.db.flush()
        self.new_message_ids.append(message.id)
        return message

    def update_card(self, message_id: str | None, card_id: str | None, status: str, result: dict | None = None) -> None:
        if not message_id or not card_id:
            return
        message = self.db.get(AssistantMessage, message_id)
        if message is None or message.conversation_id != self.conversation.id:
            return
        cards = [dict(card) for card in (message.cards or [])]
        for card in cards:
            if card.get("id") == card_id:
                card["status"] = status
                if result is not None:
                    card["result"] = result
        message.cards = cards
        flag_modified(message, "cards")
        self.db.flush()
        self.updated_message_ids.add(message.id)

    def find_card(self, card_id: str):
        """在本会话里找卡片，返回 (消息, 卡片)。"""

        for message in reversed(self.conversation.messages):
            for card in message.cards or []:
                if card.get("id") == card_id:
                    return message, card
        return None, None

    # --- 焦点与工作区 ---------------------------------------------------------------

    def set_focus(self, **values) -> None:
        focus = dict(self.conversation.focus or {})
        for key, value in values.items():
            if key not in FOCUS_KEYS:
                raise ValueError("unsupported focus key: %s" % key)
            if value is None:
                focus.pop(key, None)
            else:
                focus[key] = value
        self.conversation.focus = focus
        flag_modified(self.conversation, "focus")

    def show_workspace(self, path: str) -> None:
        """切换右侧工作区；同一路径再次调用也会让前端重新加载（`workspace_rev` 递增）。"""

        focus = dict(self.conversation.focus or {})
        focus["workspace"] = path
        focus["workspace_rev"] = int(focus.get("workspace_rev") or 0) + 1
        self.conversation.focus = focus
        flag_modified(self.conversation, "focus")
