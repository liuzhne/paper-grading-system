from datetime import datetime
from typing import Any
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import computed_field
from pydantic import model_validator

from backend.app.db.models import utcnow

MAX_FILES = 30


class AssistantModelRead(BaseModel):
    source: Literal["connection", "platform", "deployment", "none"]
    connection_id: Optional[str] = None
    label: str
    slow: bool
    notice: Optional[str] = None


class AssistantConnectionOption(BaseModel):
    id: str
    name: str
    model_name: str


class AssistantSettingsRead(BaseModel):
    configured: bool
    model_source: Optional[Literal["connection", "platform"]] = None
    ai_connection_id: Optional[str] = None
    effective: AssistantModelRead
    connections: list[AssistantConnectionOption]
    platform_available: bool
    stream_enabled: bool = False


class AssistantSettingsUpdate(BaseModel):
    model_source: Literal["connection", "platform"]
    ai_connection_id: Optional[str] = None


class AssistantMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    text: str
    cards: list[dict[str, Any]]
    intent: Optional[str] = None
    model_name: Optional[str] = None
    # 老师消息的请求编号（发送去重）：前端按它把本地消息换成后端存下的那条。助手消息为空。
    client_id: Optional[str] = None
    created_at: datetime


class AssistantPendingRead(BaseModel):
    """流程当前等待的中断：哪张卡片、需要浏览器做什么。"""

    kind: str
    card_id: Optional[str] = None
    message_id: Optional[str] = None
    # 流程线程：前端按它把内存里的待评文件归到这次流程（文件不经过后端）。
    thread_id: Optional[str] = None
    client: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class AssistantConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    focus: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    run_locked_until: Optional[datetime] = Field(default=None, exclude=True)

    @computed_field
    @property
    def running(self) -> bool:
        """这一刻有运行在推进（运行锁没过期）。流式断线后前端靠它判断还要不要等（过程流式方案 §4.4）。"""

        return self.run_locked_until is not None and self.run_locked_until > utcnow()


class AssistantConversationDetail(AssistantConversationRead):
    messages: list[AssistantMessageRead]
    pending: Optional[AssistantPendingRead] = None


class AssistantConversationCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)


class AssistantConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class AssistantAttachments(BaseModel):
    """附件只报数量与文件名：文件本身由浏览器直传（方案 T5）。"""

    count: int = Field(ge=1, le=MAX_FILES)
    names: list[str] = Field(default_factory=list, max_length=MAX_FILES)


#: 快捷按钮的键，与 `services/assistant/views.QUICK_ACTIONS` / `CHIP_ACTIONS`、前端 `lib/assistant-flow.js` 的
#: QUICK_ACTIONS / CHIP_ACTIONS 同名。
QuickAction = Literal[
    "start_grading", "import_rubric", "query_progress", "query_paper", "query_review", "query_overview", "next_steps",
]


class AssistantRunCreate(BaseModel):
    type: Literal["message", "resume", "select"]
    text: Optional[str] = Field(default=None, max_length=500)
    attachments: Optional[AssistantAttachments] = None
    # 快捷按钮发出的消息带这个键：运行器按键直接分派，不经规则与模型（Agent 化方案 §16）。
    quick_action: Optional[QuickAction] = None
    card_id: Optional[str] = Field(default=None, max_length=60)
    value: Optional[dict[str, Any]] = None
    # 请求编号（docs/评分助手发送去重方案.md）：同一会话里重复收到同一个编号时回放第一次的结果。
    # 只对 message 有意义；旧页面不带。
    client_id: Optional[str] = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")

    @model_validator(mode="after")
    def _shape(self):
        if self.type == "message":
            if not (self.text or "").strip() and self.attachments is None:
                raise ValueError("message 需要 text 或 attachments")
        elif not self.card_id:
            raise ValueError("%s 需要 card_id" % self.type)
        return self


class AssistantRunRead(BaseModel):
    messages: list[AssistantMessageRead]
    pending: Optional[AssistantPendingRead] = None
    conversation: AssistantConversationRead
