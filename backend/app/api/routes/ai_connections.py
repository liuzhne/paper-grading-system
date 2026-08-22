from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal, current_principal
from backend.app.db.models import AIConnection
from backend.app.db.session import get_db
from backend.app.schemas.ai_connection import AIConnectionCreate
from backend.app.schemas.ai_connection import AIConnectionRead
from backend.app.schemas.ai_connection import AIConnectionProbeResult
from backend.app.schemas.ai_connection import AIConnectionRotateKey
from backend.app.schemas.ai_connection import AIConnectionTestDraft
from backend.app.schemas.ai_connection import AIConnectionUpdate
from backend.app.services.ai_connections import create_connection
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import disable_connection
from backend.app.services.ai_connections import enforce_connection_rate_limit
from backend.app.services.ai_connections import key_masked
from backend.app.services.ai_connections import rotate_connection_key
from backend.app.services.ai_connections import resolve_connection_runtime
from backend.app.services.ai_connections import validate_base_url
from backend.app.services.ai_connections import validate_provider_options
from backend.app.services.ai_connections import verify_connection_runtime
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
    try:
        connection = create_connection(
            db,
            owner_id=principal.user_id,
            organization_id=_organization_id(principal),
            **payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        db,
        "ai_connection.created",
        actor_id=principal.user_id,
        organization_id=connection.organization_id,
        metadata={"connection_id": connection.id, "provider_type": connection.provider_type},
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
        runtime = ConnectionRuntime(
            connection_id="draft",
            key_version=0,
            organization_id=organization_id,
            provider_type=payload.provider_type,
            base_url=validate_base_url(payload.base_url),
            model_name=payload.model_name.strip(),
            provider_options=validate_provider_options(payload.provider_options),
            api_key=payload.api_key,
        )
        verified = verify_connection_runtime(runtime)
    except ValueError as exc:
        audit(db, "ai_connection.draft_test_failed", actor_id=principal.user_id, organization_id=organization_id, metadata={"provider_type": payload.provider_type})
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "ai_connection.draft_tested", actor_id=principal.user_id, organization_id=organization_id, metadata={"provider_type": payload.provider_type, "model_name": payload.model_name})
    db.commit()
    return {**verified, "status": "verified"}


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
            connection.base_url = validate_base_url(payload.base_url)
        if payload.model_name is not None:
            connection.model_name = payload.model_name.strip()
        if payload.provider_options is not None:
            connection.provider_options = validate_provider_options(payload.provider_options)
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
