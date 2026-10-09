from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentPrincipal, current_principal
from backend.app.db.models import AIConnection
from backend.app.db.session import get_db
from backend.app.schemas.ai_connection import AIConnectionCreate
from backend.app.schemas.ai_connection import AIConnectionRead
from backend.app.schemas.ai_connection import AIConnectionProbeResult
from backend.app.schemas.ai_connection import AIConnectionRotateKey
from backend.app.schemas.ai_connection import AIConnectionTestDraft
from backend.app.schemas.ai_connection import AIConnectionUpdate
from backend.app.services.ai_connections import activate_connection, lock_connection_owner
from backend.app.services.ai_connections import create_connection
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import disable_connection
from backend.app.services.ai_connections import enforce_connection_rate_limit
from backend.app.services.ai_connections import key_masked
from backend.app.services.ai_connections import rotate_connection_key
from backend.app.services.ai_connections import resolve_connection_runtime
from backend.app.services.ai_connections import normalize_connection_base_url
from backend.app.services.ai_connections import validate_base_url
from backend.app.services.ai_connections import validate_provider_options
from backend.app.services.ai_connections import validate_provider_options_for
from backend.app.services.ai_connections import verify_connection_runtime
from backend.app.services.ai_connection_protocol import ProtocolNotDetected
from backend.app.services.ai_connection_protocol import ProtocolResolution
from backend.app.services.ai_connection_protocol import resolve_protocol
from backend.app.db.models import utcnow
from backend.app.services.auth import audit


router = APIRouter(prefix="/ai-connections", tags=["ai-connections"])


def _organization_id(principal: CurrentPrincipal) -> str:
    if not principal.organization_id:
        raise HTTPException(status_code=400, detail="an organization is required for AI connections")
    return principal.organization_id


def _enforce_rate_limit(principal: CurrentPrincipal) -> None:
    try:
        enforce_connection_rate_limit(principal.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


def _resolve_protocol(payload: AIConnectionCreate, organization_id: str, *, verify: bool) -> ProtocolResolution:
    """Validate the draft fields, then decide the protocol (see ai_connection_protocol)."""

    base_url = validate_base_url(payload.base_url)
    options = validate_provider_options(payload.provider_options)
    api_key = payload.api_key.strip()
    if len(api_key) < 4:
        raise ValueError("API key must contain at least four characters")

    def probe(provider_type: str, candidate_base_url: str) -> None:
        # 协议相关的参数校验在发请求之前：Claude 专属参数不能发给 Chat/Responses，反之亦然。
        validate_provider_options_for(provider_type, options)
        verify_connection_runtime(ConnectionRuntime(
            connection_id="draft",
            key_version=0,
            organization_id=organization_id,
            provider_type=provider_type,
            base_url=candidate_base_url,
            model_name=payload.model_name.strip(),
            provider_options=options,
            api_key=api_key,
        ))

    try:
        return resolve_protocol(requested=payload.provider_type, base_url=base_url, verify=verify, probe=probe)
    except ProtocolNotDetected:
        raise
    except ValueError as exc:
        if verify or payload.provider_type != "auto":
            raise
        # 保存时只有未知平台才会探测；探测失败多半是地址、模型或 Key 的问题，不是协议。
        raise ValueError(
            "无法自动识别协议：测试请求失败，请检查接口地址、模型和 API Key，或在高级设置中手动选择协议。"
        ) from exc


def _read(connection: AIConnection) -> dict:
    return {
        "id": connection.id,
        "organization_id": connection.organization_id,
        "owner_id": connection.owner_id,
        "name": connection.name,
        "scope": connection.scope,
        "provider_type": connection.provider_type,
        "base_url": connection.base_url,
        "model_name": connection.model_name,
        "provider_options": connection.provider_options or {},
        "key_version": connection.key_version,
        "key_last4": connection.key_last4,
        "key_masked": key_masked(connection.key_last4),
        "status": connection.status,
        "last_verified_at": connection.last_verified_at,
        "last_error_code": connection.last_error_code,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
        "disabled_at": connection.disabled_at,
    }


def _owned_connection(db: Session, connection_id: str, principal: CurrentPrincipal) -> AIConnection:
    lock_connection_owner(db, principal.user_id)
    connection = db.scalar(
        select(AIConnection).where(
            AIConnection.id == connection_id,
            AIConnection.owner_id == principal.user_id,
            AIConnection.organization_id == _organization_id(principal),
            AIConnection.status != "deleted",
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="AI connection not found")
    return connection


@router.get("", response_model=list[AIConnectionRead])
def list_connections(
    db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)
):
    return [
        _read(connection)
        for connection in db.scalars(
            select(AIConnection)
            .where(
                AIConnection.owner_id == principal.user_id,
                AIConnection.organization_id == _organization_id(principal),
                AIConnection.status != "deleted",
            )
            .order_by(AIConnection.created_at.desc())
        )
    ]


@router.post("", response_model=AIConnectionRead, status_code=201)
def create_ai_connection(
    payload: AIConnectionCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    organization_id = _organization_id(principal)
    try:
        resolution = _resolve_protocol(payload, organization_id, verify=False)
        connection = create_connection(
            db,
            owner_id=principal.user_id,
            organization_id=organization_id,
            **{**payload.model_dump(), "provider_type": resolution.provider_type, "base_url": resolution.base_url},
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="AI connections changed; refresh and retry") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if resolution.verified:
        # 未知平台的识别本身就是一次成功的服务端探测。
        connection.last_verified_at = utcnow()
    audit(
        db,
        "ai_connection.created",
        actor_id=principal.user_id,
        organization_id=connection.organization_id,
        metadata={"connection_id": connection.id, "provider_type": connection.provider_type,
                  "protocol_detection": resolution.source},
    )
    db.commit()
    db.refresh(connection)
    return _read(connection)


@router.post("/test-draft", response_model=AIConnectionProbeResult)
def test_draft_connection(
    payload: AIConnectionTestDraft,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    organization_id = _organization_id(principal)
    try:
        resolution = _resolve_protocol(payload, organization_id, verify=True)
    except ValueError as exc:
        audit(db, "ai_connection.draft_test_failed", actor_id=principal.user_id, organization_id=organization_id, metadata={"provider_type": payload.provider_type})
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "ai_connection.draft_tested", actor_id=principal.user_id, organization_id=organization_id,
          metadata={"provider_type": resolution.provider_type, "model_name": payload.model_name,
                    "protocol_detection": resolution.source})
    db.commit()
    return {
        "provider_type": resolution.provider_type,
        "model_name": payload.model_name.strip(),
        "status": "verified",
        "detection": resolution.source,
        "base_url": resolution.base_url,
    }


@router.post("/{connection_id}/test", response_model=AIConnectionProbeResult)
def test_saved_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    connection = _owned_connection(db, connection_id, principal)
    try:
        runtime = resolve_connection_runtime(
            db,
            connection_id=connection.id,
            owner_id=principal.user_id,
            organization_id=connection.organization_id,
            allow_disabled=True,
        )
        verified = verify_connection_runtime(runtime)
    except ValueError as exc:
        connection.last_error_code = "CONNECTION_TEST_FAILED"
        audit(db, "ai_connection.verification_failed", actor_id=principal.user_id, organization_id=connection.organization_id, metadata={"connection_id": connection.id, "error_code": connection.last_error_code})
        db.commit()
        raise HTTPException(status_code=400, detail="AI connection test failed") from exc
    connection.last_verified_at = utcnow()
    connection.last_error_code = None
    audit(db, "ai_connection.verified", actor_id=principal.user_id, organization_id=connection.organization_id, metadata={"connection_id": connection.id})
    db.commit()
    return {**verified, "status": "verified"}


@router.patch("/{connection_id}", response_model=AIConnectionRead)
def update_ai_connection(
    connection_id: str,
    payload: AIConnectionUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    connection = _owned_connection(db, connection_id, principal)
    try:
        if payload.name is not None:
            connection.name = payload.name.strip()
        if payload.base_url is not None:
            connection.base_url = normalize_connection_base_url(connection.provider_type, payload.base_url)
        if payload.model_name is not None:
            connection.model_name = payload.model_name.strip()
        if payload.provider_options is not None:
            connection.provider_options = validate_provider_options_for(
                connection.provider_type, payload.provider_options
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "ai_connection.updated", actor_id=principal.user_id, organization_id=connection.organization_id, metadata={"connection_id": connection.id})
    db.commit()
    db.refresh(connection)
    return _read(connection)


@router.post("/{connection_id}/rotate-key", response_model=AIConnectionRead)
def rotate_ai_connection_key(
    connection_id: str,
    payload: AIConnectionRotateKey,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    connection = _owned_connection(db, connection_id, principal)
    try:
        rotate_connection_key(connection, payload.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "ai_connection.key_rotated", actor_id=principal.user_id, organization_id=connection.organization_id, metadata={"connection_id": connection.id, "key_version": connection.key_version})
    db.commit()
    db.refresh(connection)
    return _read(connection)


@router.post("/{connection_id}/activate", response_model=AIConnectionRead)
def activate_ai_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    connection = _owned_connection(db, connection_id, principal)
    try:
        activate_connection(db, connection)
        audit(db, "ai_connection.activated", actor_id=principal.user_id,
              organization_id=connection.organization_id,
              metadata={"connection_id": connection.id})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="AI connections changed; refresh and retry") from exc
    db.refresh(connection)
    return _read(connection)


@router.post("/{connection_id}/disable", response_model=AIConnectionRead)
def disable_ai_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    connection = _owned_connection(db, connection_id, principal)
    disable_connection(connection)
    audit(db, "ai_connection.disabled", actor_id=principal.user_id, organization_id=connection.organization_id, metadata={"connection_id": connection.id})
    db.commit()
    db.refresh(connection)
    return _read(connection)


@router.delete("/{connection_id}", status_code=204)
def delete_ai_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _enforce_rate_limit(principal)
    connection = _owned_connection(db, connection_id, principal)
    connection.status = "deleted"
    connection.deleted_at = connection.updated_at
    audit(db, "ai_connection.deleted", actor_id=principal.user_id, organization_id=connection.organization_id, metadata={"connection_id": connection.id})
    db.commit()
    return Response(status_code=204)
