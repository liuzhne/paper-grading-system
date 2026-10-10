"""AI 操作异步任务：提交后立即返回任务，刷新或关页面不影响执行。"""

import logging
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from fastapi import UploadFile
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
from backend.app.api.routes.rubrics import _rubric_problem
from backend.app.api.routes.rubrics import _visible_rubric
from backend.app.db.session import get_db
from backend.app.schemas.ai_task import AITaskCreate
from backend.app.schemas.ai_task import AITaskRead
from backend.app.services.ai_tasks.errors import AITaskProblem
from backend.app.services.ai_tasks.execution import KIND as AI_TASK_KIND
from backend.app.services.ai_tasks.service import cancel_ai_task
from backend.app.services.ai_tasks.service import create_ai_task
from backend.app.services.ai_tasks.service import get_ai_task
from backend.app.services.ai_tasks.service import list_ai_tasks
from backend.app.services.ai_tasks.service import retry_ai_task
from backend.app.services.ai_tasks.state import ACTIVE_TASK_STATUSES
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.rubric_import import parse_state
from backend.app.services.rubric_import import structure_state
from backend.app.services.work_queue.sweep import ensure_sweep_chain
from backend.app.services.work_queue.sweep import sweep_parent_on_read
from backend.app.services.work_queue.wake import wake_for_capacity


router = APIRouter(tags=["ai-tasks"])
logger = logging.getLogger("ai-tasks")


def _problem(exc: AITaskProblem) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=_rubric_problem(
            code=exc.code,
            message=exc.message,
            user_action=exc.user_action,
            retryable=exc.retryable,
            context=exc.context,
        ),
    )


def _session_factory(db):
    bind = db.get_bind()
    db.rollback()
    return sessionmaker(bind=bind, autocommit=False, autoflush=False)


def _wake(db, task, *, reason):
    """按空闲名额叫醒（Vercel）；没发出去也不影响任务，巡检会补发。"""

    source_key = next((item.source_key for item in task.items), None)
    token = "%s-%s" % (task.id, task.state_version)
    factory = _session_factory(db)
    try:
        if source_key:
            with factory() as session:
                wake_for_capacity(session, source_key, reason=reason, token=token)
        ensure_sweep_chain(factory)
    except Exception:
        logger.exception("ai_task_wake_failed task_id=%s", task.id)


def _task_or_404(db, task_id, principal):
    task = get_ai_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="AI task not found")
    if task.rubric_id is None:
        # 导入前的结构识别还没有评分标准：只对建任务的用户可见。
        if task.owner_id != principal.user_id:
            raise HTTPException(status_code=404, detail="AI task not found")
    else:
        # 任务只对能看到这份评分标准、且同组织的用户可见。
        _visible_rubric(db, task.rubric_id, principal)
    if (
        principal.organization_id is not None
        and task.organization_id is not None
        and task.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="AI task not found")
    return task


@router.post(
    "/rubrics/{rubric_id}/ai-tasks",
    response_model=AITaskRead,
    status_code=202,
)
def create_task(
    rubric_id: str,
    payload: AITaskCreate,
    response: Response,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """新建返回 202；命中同指纹的进行中或已成功任务返回 200 和已有任务。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        task, created = create_ai_task(
            db,
            rubric_id=rubric_id,
            kind=payload.kind,
            params=payload.params,
            principal=principal,
            ai_connection_id=payload.ai_connection_id,
            regenerate=payload.regenerate,
        )
    except AITaskProblem as exc:
        db.rollback()
        raise _problem(exc) from exc
    task_id = task.id
    if created and task.status in ACTIVE_TASK_STATUSES:
        _wake(db, task, reason="create")
    response.status_code = 202 if created else 200
    return get_ai_task(db, task_id)


@router.post("/ai-tasks/import-structure", response_model=AITaskRead, status_code=202)
def create_import_structure_task(
    response: Response,
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    ai_connection_id: Optional[str] = Form(None),
    regenerate: bool = Form(False),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """导入前（E1/E7 识别失败、还没有评分标准）的表格结构识别任务。

    上传文件在这里解析成台账后冻结进任务，文件本身不落库；任务只对建任务的用户可见，
    结果（结构与将导入的评分项）在任务里，确认后带 ``structure_override`` 调用导入接口。
    """

    ensure_dev_user(db)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        upload = structure_state.import_structure_request(
            rules_bytes=rules_file.file.read() if rules_file else None,
            template_bytes=template_file.file.read() if template_file else None,
        )
    except parse_state.ParseStateError as exc:
        raise _problem(
            AITaskProblem(exc.status, exc.code, exc.message, "请检查上传的文件后重试。")
        ) from exc
    try:
        task, created = create_ai_task(
            db,
            rubric_id=None,
            kind="structure_suggestion",
            params={"upload": upload},
            principal=principal,
            ai_connection_id=ai_connection_id,
            regenerate=regenerate,
        )
    except AITaskProblem as exc:
        db.rollback()
        raise _problem(exc) from exc
    task_id = task.id
    if created and task.status in ACTIVE_TASK_STATUSES:
        _wake(db, task, reason="create")
    response.status_code = 202 if created else 200
    return get_ai_task(db, task_id)


@router.get("/rubrics/{rubric_id}/ai-tasks", response_model=list[AITaskRead])
def list_tasks(
    rubric_id: str,
    kind: Optional[str] = Query(default=None),
    active: bool = Query(default=False),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """刷新页面后找回进行中的任务（``active=1``）。"""

    _visible_rubric(db, rubric_id, principal)
    tasks = list_ai_tasks(db, rubric_id, kind=kind, active=active)
    if principal.organization_id is not None:
        tasks = [
            task
            for task in tasks
            if task.organization_id is None or task.organization_id == principal.organization_id
        ]
    return tasks


@router.get("/ai-tasks/{task_id}", response_model=AITaskRead)
def read_task(
    task_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """状态、进度与各条目状态；成功时附结果。对本任务做一次限频巡检。"""

    task = _task_or_404(db, task_id, principal)
    if task.status in ACTIVE_TASK_STATUSES:
        try:
            sweep_parent_on_read(_session_factory(db), AI_TASK_KIND, task_id)
        except Exception:
            logger.exception("ai_task_read_sweep_failed task_id=%s", task_id)
        db.expire_all()
        task = _task_or_404(db, task_id, principal)
    return task


@router.post("/ai-tasks/{task_id}/cancel", response_model=AITaskRead)
def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _task_or_404(db, task_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        return cancel_ai_task(db, task_id)
    except AITaskProblem as exc:
        db.rollback()
        raise _problem(exc) from exc


@router.post("/ai-tasks/{task_id}/retry", response_model=AITaskRead)
def retry_task(
    task_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """只重试失败的条目，已成功的保留；期间已有同内容的任务时返回那个任务。"""

    _task_or_404(db, task_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        task, _replaced = retry_ai_task(db, task_id)
    except AITaskProblem as exc:
        db.rollback()
        raise _problem(exc) from exc
    result_id = task.id
    if task.status in ACTIVE_TASK_STATUSES:
        _wake(db, task, reason="retry")
    return get_ai_task(db, result_id)
