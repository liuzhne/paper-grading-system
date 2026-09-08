from datetime import datetime
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class BatchCreate(BaseModel):
    name: str = Field(min_length=1)
    rubric_id: str
    rubric_version_id: Optional[str] = None
    ai_connection_id: Optional[str] = None
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    # 创建只允许 draft（前端 v2 计划 §5-C）。旧客户端显式传 "draft" 仍可用；
    # 任何其它阶段必须走动作服务，不能由客户端在创建时直接指定。
    status: Literal["draft"] = "draft"
    # 首版只做单评（§5-G，决策 4）。**只校验，不持久化**——存一个不影响任何行为
    # 的值，会让人以为这批是按那个模式评的。
    #
    # 静默忽略这个字段是最糟的选择：调用方发 "dual" 拿到 200，据此认为系统在做
    # 双评，而实际上每份材料只评了一次，错误结论会一直被当成双评结果用。
    review_mode: Literal["single"] | None = Field(
        default=None,
        description="首版仅支持 single；双评与仲裁是独立里程碑，尚未实现。",
    )


class BatchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    rubric_id: Optional[str] = None
    rubric_version_id: Optional[str] = None
    ai_connection_id: Optional[str] = None
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    status: Optional[str] = None


class BatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    rubric_id: str
    rubric_version_id: Optional[str] = None
    ai_connection_id: Optional[str] = None
    ai_connection_key_version: Optional[int] = None
    ai_connection_snapshot: Optional[dict] = None
    status: str
    # 乐观并发游标：客户端带回它，过期的写请求会冲突失败而非静默覆盖。
    state_version: int
    owner_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BatchScoreResult(BaseModel):
    batch_id: str
    total_papers: int
    scored_count: int
    skipped_count: int
    failed_count: int
    run_ids: list[str]
    errors: list[dict]


class BatchPaperSummary(BaseModel):
    paper_id: str
    title: Optional[str] = None
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    file_name: str
    paper_status: str
    parse_quality: Optional[float] = None
    latest_run_id: Optional[str] = None
    latest_run_status: Optional[str] = None
    latest_final_score: Optional[float] = None
    latest_grade: Optional[str] = None
    latest_need_manual_review: Optional[bool] = None


class BatchSummary(BaseModel):
    batch: BatchRead
    rubric_name: str
    rubric_version: str
    paper_stats: dict[str, int]
    run_stats: dict[str, int]
    papers: list[BatchPaperSummary]


class BatchProgressCounts(BaseModel):
    """当前结果选择集合下的分阶段计数；分母是本轮目标材料，不是批次全部材料。"""

    total: int
    scored: int
    reviewed: int
    failed: int
    pending: int


class BatchProgressJob(BaseModel):
    """执行状态，与业务阶段并行返回。"""

    id: str
    generation: int
    status: str
    total_items: int
    succeeded_count: int
    failed_count: int
    pending_count: int


class BatchProgressRead(BaseModel):
    batch_id: str
    stage: str
    state_version: int
    counts: BatchProgressCounts
    #: 空批次为 None——不编造百分比。
    completion_ratio: Optional[float] = None
    result_revision: str
    job: Optional[BatchProgressJob] = None
    #: 服务端给出的可执行动作，前端不自行推断转移合法性。
    available_actions: list[str]


class BatchOverviewMaterials(BaseModel):
    total: int
    scored: int
    reviewed: int
    failed: int
    pending: int


class BatchOverviewRead(BaseModel):
    """工作台 KPI。与 `GET /batches` 的数组合同分开，避免为加字段破坏旧客户端。"""

    total_batches: int
    #: 待办口径：不含已归档与已复核。
    active_batches: int
    stage_counts: dict[str, int]
    material_counts: BatchOverviewMaterials


class ReviewAcceptItem(BaseModel):
    score_item_id: str
    review_revision: int = Field(ge=1)


class ReviewAcceptRequest(BaseModel):
    """明确的有限集合。服务端不做无界全批扫描（计划 §5-B）。"""

    result_revision: str = Field(min_length=16)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1)
    items: list[ReviewAcceptItem] = Field(min_length=1, max_length=100)


class ReviewAcceptResult(BaseModel):
    batch_id: str
    accepted_count: int
    result_revision: str
    #: 幂等重放；未产生新的复核记录。
    replayed: bool


class BatchStageActionRequest(BaseModel):
    """归档 / 重新打开。

    只带 `state_version`，**不带目标阶段**：目标由服务端从状态机推导。让客户端
    指定目标等于把「重开后回到哪一步」交给一个可能已经过期的页面去决定。
    """

    state_version: int = Field(ge=0)


class CompleteReviewRequest(BaseModel):
    """完成复核。带上结果集合 revision，过期请求不得完成复核。"""

    result_revision: str = Field(min_length=16)


class UploadPrecheckRequest(BaseModel):
    """只接收已归档的 paper ID；预检不重传文件也不重新解析（计划 §5-E）。"""

    paper_ids: list[str] = Field(min_length=1, max_length=500)


#: 三种状态（计划 §5-F）。**没有「已下载」**——客户端断开证明不了文件已落地，
#: 把「已生成」写成「已下载」是拿一个查不到的事实充数。
EXPORT_EVENT_STATUSES = ("generating", "generated", "failed")


class ExportEventCreate(BaseModel):
    """记录一次导出请求。"""

    channel: Literal["html_report", "xlsx", "json", "sheets", "mock_sheet"]
    scope: str = Field(min_length=1, max_length=50)
    result_revision: str = Field(min_length=16)
    #: 默认 generated，旧客户端不传也照常工作。
    status: Literal["generating", "generated", "failed"] = "generated"
    error_message: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_matches_status(self):
        # 「失败了但不知道为什么」对对账毫无用处；而挂着错误信息的成功记录会让
        # 人去追一个不存在的故障。
        if self.status == "failed" and not (self.error_message or "").strip():
            raise ValueError("status=failed 必须给出 error_message")
        if self.status != "failed" and self.error_message is not None:
            raise ValueError("只有 status=failed 才能带 error_message")
        return self


class ExportEventUpdate(BaseModel):
    """把「生成中」的事件定案。"""

    status: Literal["generated", "failed"]
    error_message: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_matches_status(self):
        if self.status == "failed" and not (self.error_message or "").strip():
            raise ValueError("status=failed 必须给出 error_message")
        if self.status != "failed" and self.error_message is not None:
            raise ValueError("只有 status=failed 才能带 error_message")
        return self
