"""评分助手接口（设计方案 docs/对话评分助手改造方案.md §11）。

助手本身不评分、不改分。流程由后端的 LangGraph 图推进（`services/assistant/runner.py`），
写动作经共享守卫与共享动作执行，与页面接口同一权限边界。这里只做鉴权、会话
归属与请求转发。
"""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_organization_role
from backend.app.core.config import settings
from backend.app.db.models import AssistantConversation
from backend.app.db.models import AssistantPreference
from backend.app.db.models import utcnow
from backend.app.db.session import get_db
from backend.app.schemas.assistant import AssistantConversationCreate
from backend.app.schemas.assistant import AssistantConversationDetail
from backend.app.schemas.assistant import AssistantConversationRead
from backend.app.schemas.assistant import AssistantConversationUpdate
from backend.app.schemas.assistant import AssistantRunCreate
from backend.app.schemas.assistant import AssistantRunRead
from backend.app.schemas.assistant import AssistantSettingsRead
from backend.app.schemas.assistant import AssistantSettingsUpdate
from backend.app.services.assistant import model as assistant_model
from backend.app.services.assistant import runner
from backend.app.services.assistant import stream
from backend.app.services.dev_user import ensure_dev_user


def _no_store(response: Response) -> None:
    # 消息与卡片里可能有学生姓名、文件名：与证据接口一样不进任何缓存。
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(_no_store)])

DEFAULT_TITLE = "新对话"
MAX_CONVERSATIONS_LISTED = 100



def _principal(db: Session, principal: CurrentPrincipal) -> CurrentPrincipal:
    ensure_dev_user(db)
    # 一期只面向老师与管理员（维护者决定 U1）；学生角色的查询在一期之后加入。
    require_organization_role(principal, "org_admin", "teacher")
    return principal


def _owned_conversation(db: Session, conversation_id: str, principal: CurrentPrincipal):
    query = select(AssistantConversation).where(
        AssistantConversation.id == conversation_id,
        AssistantConversation.owner_id == principal.user_id,
    )
    if principal.organization_id is None:
        query = query.where(AssistantConversation.organization_id.is_(None))
    else:
        query = query.where(AssistantConversation.organization_id == principal.organization_id)
    conversation = db.scalar(query)
    if conversation is None:
        # 别人的会话与不存在的会话不可区分：消息里有学生信息。
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


def _settings_payload(db: Session, principal: CurrentPrincipal) -> dict:
    preference = assistant_model.get_preference(
        db, user_id=principal.user_id, organization_id=principal.organization_id
    )
    connections = assistant_model.active_connections(
        db, user_id=principal.user_id, organization_id=principal.organization_id
    )
    effective = assistant_model.effective_model(
        db, user_id=principal.user_id, organization_id=principal.organization_id
    )
    return {
        "configured": preference is not None,
        "model_source": preference.model_source if preference else None,
        "ai_connection_id": preference.ai_connection_id if preference else None,
        "effective": effective.as_dict(),
        "connections": [
            {"id": item.id, "name": item.name, "model_name": item.model_name}
            for item in connections
        ],
        "platform_available": assistant_model.platform_available(db),
        # 前端据此选 `/runs/stream` 还是 `/runs`（docs/评分助手过程流式方案.md §4.5）。
        "stream_enabled": settings.ASSISTANT_STREAM_ENABLED,
    }


@router.get("/settings", response_model=AssistantSettingsRead)
def read_settings(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    return _settings_payload(db, principal)


@router.put("/settings", response_model=AssistantSettingsRead)
def update_settings(
    payload: AssistantSettingsUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    connection_id = None
    if payload.model_source == "connection":
        owned = {
            item.id
            for item in assistant_model.active_connections(
                db, user_id=principal.user_id, organization_id=principal.organization_id
            )
        }
        if not payload.ai_connection_id or payload.ai_connection_id not in owned:
            raise HTTPException(status_code=422, detail="请选择一条自己的、已启用的模型连接。")
        connection_id = payload.ai_connection_id
    elif not assistant_model.platform_available(db):
        raise HTTPException(status_code=422, detail="平台模型尚未配置，请选择自己的模型连接。")

    preference = assistant_model.get_preference(
        db, user_id=principal.user_id, organization_id=principal.organization_id
    )
    if preference is None:
        preference = AssistantPreference(
            user_id=principal.user_id, organization_id=principal.organization_id,
            model_source=payload.model_source,
        )
        db.add(preference)
    preference.model_source = payload.model_source
    preference.ai_connection_id = connection_id
    preference.updated_at = utcnow()
    db.commit()
    return _settings_payload(db, principal)


@router.get("/conversations", response_model=list[AssistantConversationRead])
def list_conversations(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    query = select(AssistantConversation).where(
        AssistantConversation.owner_id == principal.user_id
    )
    if principal.organization_id is None:
        query = query.where(AssistantConversation.organization_id.is_(None))
    else:
        query = query.where(AssistantConversation.organization_id == principal.organization_id)
    return db.scalars(
        query.order_by(AssistantConversation.updated_at.desc(), AssistantConversation.id.desc())
        .limit(MAX_CONVERSATIONS_LISTED)
    ).all()


@router.post("/conversations", response_model=AssistantConversationDetail, status_code=201)
def create_conversation(
    payload: AssistantConversationCreate | None = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    title = ((payload.title if payload else None) or "").strip() or DEFAULT_TITLE
    conversation = AssistantConversation(
        organization_id=principal.organization_id,
        owner_id=principal.user_id,
        title=title[:200],
        focus={},
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/conversations/{conversation_id}", response_model=AssistantConversationDetail)
def read_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    conversation = _owned_conversation(db, conversation_id, principal)
    runner.expire_if_stale(db, principal, conversation)
    return {
        "id": conversation.id, "title": conversation.title, "focus": conversation.focus,
        "created_at": conversation.created_at, "updated_at": conversation.updated_at,
        # 只用来算 `running`，不出现在返回里：流式断线后前端靠它判断还要不要等。
        "run_locked_until": conversation.run_locked_until,
        "messages": conversation.messages, "pending": conversation.pending,
    }


@router.patch("/conversations/{conversation_id}", response_model=AssistantConversationRead)
def update_conversation(
    conversation_id: str,
    payload: AssistantConversationUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    conversation = _owned_conversation(db, conversation_id, principal)
    conversation.title = payload.title.strip()[:200] or DEFAULT_TITLE
    conversation.updated_at = utcnow()
    db.commit()
    db.refresh(conversation)
    return conversation


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    principal = _principal(db, principal)
    conversation = _owned_conversation(db, conversation_id, principal)
    runner.delete_conversation_state(db, conversation)
    db.delete(conversation)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/conversations/{conversation_id}/runs",
    response_model=AssistantRunRead,
)
def create_run(
    conversation_id: str,
    payload: AssistantRunCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """推进一次：用户输入（message）、恢复流程（resume）或操作选择卡片（select）。"""

    principal = _principal(db, principal)
    conversation = _owned_conversation(db, conversation_id, principal)
    return runner.run(db, principal, conversation, _run_request(payload))


def _run_request(payload: AssistantRunCreate) -> dict:
    request = payload.model_dump()
    if payload.type == "message":
        text = (payload.text or "").strip()
        if not text and payload.attachments is not None:
            text = "评分这 %d 份文件" % payload.attachments.count
        request["text"] = text
    return request


def _prepare_stream(db: Session, principal: CurrentPrincipal, conversation_id: str, payload: AssistantRunCreate):
    """开始推送之前做完的事：鉴权、会话归属、请求整理、抢运行锁。不通过的照旧返回 404 / 409 / 422。"""

    principal = _principal(db, principal)
    conversation = _owned_conversation(db, conversation_id, principal)
    request = _run_request(payload)
    runner.acquire_lock(db, conversation)
    bind = db.get_bind()
    # 后台线程另开会话。请求的会话现在就关：它在请求结束时才会被依赖关闭，那时的回滚若落在同一条连接上
    # （测试的单连接内存库），会把后台线程还没提交的事务一起回滚掉。
    db.close()
    return principal, conversation.id, request, bind


@router.post(
    "/conversations/{conversation_id}/runs/stream",
    response_class=StreamingResponse,
    responses={200: {
        "description": "SSE：accepted → progress（零到多条）→ done（与 /runs 的返回体相同）或 error；空闲时有注释行心跳",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }},
)
async def create_run_stream(
    conversation_id: str,
    payload: AssistantRunCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """与 `/runs` 相同的一次推进，过程中推送进度（docs/评分助手过程流式方案.md）。开关关闭时 404。"""

    if not settings.ASSISTANT_STREAM_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    principal, conversation_id, request, bind = await run_in_threadpool(
        _prepare_stream, db, principal, conversation_id, payload
    )
    return StreamingResponse(
        stream.start(bind, principal, conversation_id, request),
        media_type="text/event-stream",
        # 直接返回 Response 时，路由依赖设置的响应头不会带上，这里自己写。
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )
