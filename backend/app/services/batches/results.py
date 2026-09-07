"""当前结果选择器（前端 v2 计划 §5-B）。

**同一批次在不同页面上必须给出同一组数字。** 队列、KPI、复核统计、分布图与
导出预检都从这里取「哪些材料算数、每份材料哪个 run 是当前结果」，否则工作台
说 18 份已确认、复核页说 16 份，用户无从判断哪个是真的。

两条容易做错的判据：

1. **重评期间不能沿用旧结果。** 一旦有新的 generation 在跑，尚未产出新 run
   的材料是「待评」，不是「已评分」——显示上一轮的分数会让人以为重评已完成。
2. **排序必须确定。** 同一时刻创建的两个 run 若按不稳定顺序取「最新」，统计
   会在两次请求之间跳变。这里固定用 ``(created_at, id)``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from sqlalchemy import select

from backend.app.db.models import BatchScoringItem
from backend.app.db.models import BatchScoringJob
from backend.app.db.models import Paper
from backend.app.db.models import ScoringRun


#: run.status 中代表「已产出有效结果」的取值。
SCORED_STATUSES = frozenset({"scored", "reviewed"})

#: 选择集合摘要的版本号；口径变化时同步 bump，避免新旧 revision 被误认为等价。
REVISION_SCHEMA = "batch-result-selection@1"


@dataclass(frozen=True)
class ResultSelection:
    """一个批次在某一时刻的「当前结果」快照。"""

    batch_id: str
    #: 当前批任务 generation；无批任务时为 None。
    generation: int | None
    #: 纳入统计的材料，保持确定顺序。
    paper_ids: tuple[str, ...]
    #: paper_id -> 当前结果 run_id；待评材料不出现在此映射中。
    runs: dict = field(default_factory=dict)
    #: 各 run 的状态，供计数使用。
    run_statuses: dict = field(default_factory=dict)
    revision: str = ""

    def run_id_for(self, paper_id):
        return self.runs.get(paper_id)

    @property
    def total_count(self):
        return len(self.paper_ids)

    @property
    def scored_count(self):
        return sum(
            1 for status in self.run_statuses.values() if status in SCORED_STATUSES
        )

    @property
    def reviewed_count(self):
        return sum(1 for status in self.run_statuses.values() if status == "reviewed")

    @property
    def failed_count(self):
        return sum(1 for status in self.run_statuses.values() if status == "failed")

    @property
    def pending_count(self):
        return self.total_count - len(self.runs)

    @property
    def completion_ratio(self):
        """已出结果的占比；空批次返回 None，不编造百分比。"""
        if not self.total_count:
            return None
        return self.scored_count / self.total_count


def current_job(session, batch_id):
    """取最新一次 generation 的批任务；没有则返回 None。"""
    return session.scalar(
        select(BatchScoringJob)
        .where(BatchScoringJob.grading_batch_id == batch_id)
        .order_by(BatchScoringJob.generation.desc(), BatchScoringJob.id.desc())
        .limit(1)
    )


#: 本轮尚未产出结论的 item 状态。处于这些状态的材料一律算「待评」，
#: 即使它还留着上一轮的 run——显示旧分数会让人误以为重评已完成。
UNFINISHED_ITEM_STATUSES = frozenset({"pending", "running", "canceled"})


def _scope(session, batch_id, job):
    """本次统计的材料范围，以及每份材料在本轮是否已有结论。

    有批任务时以该 generation 的 item 为准——重评可能只覆盖部分材料，用整批
    材料当分母会把没参与本轮的材料算成「待评」。

    是否「本轮已完成」取自 item 状态而非时间戳比较：后者依赖墙钟，任务重启、
    时钟回拨或补录数据都会让它给出错误答案。
    """
    if job is not None:
        items = session.scalars(
            select(BatchScoringItem)
            .where(BatchScoringItem.job_id == job.id)
            .order_by(BatchScoringItem.paper_id)
        ).all()
        if items:
            return (
                tuple(item.paper_id for item in items),
                {
                    item.paper_id: item.status not in UNFINISHED_ITEM_STATUSES
                    for item in items
                },
            )
    papers = session.scalars(
        select(Paper).where(Paper.batch_id == batch_id).order_by(Paper.id)
    ).all()
    return tuple(paper.id for paper in papers), None


def _latest_run(runs):
    """确定性地取最新 run：先比 created_at，再比 id。"""
    if not runs:
        return None
    return max(runs, key=lambda run: (run.created_at, run.id))


def _compute_revision(batch_id, generation, pairs):
    """选择集合的稳定摘要。

    只暴露摘要而不是 id 清单：客户端拿它做写操作的前置条件即可，没有必要
    把内部标识铺开。
    """
    payload = "|".join(
        [REVISION_SCHEMA, batch_id, str(generation)]
        + ["%s=%s" % (paper_id, run_id or "") for paper_id, run_id in pairs]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_current_results(session, batch) -> ResultSelection:
    job = current_job(session, batch.id)
    paper_ids, finished_in_generation = _scope(session, batch.id, job)
    generation = job.generation if job is not None else None

    runs = {}
    run_statuses = {}
    if paper_ids:
        rows = session.scalars(
            select(ScoringRun).where(ScoringRun.paper_id.in_(paper_ids))
        ).all()
        by_paper = {}
        for run in rows:
            by_paper.setdefault(run.paper_id, []).append(run)

        for paper_id in paper_ids:
            if finished_in_generation is not None and not finished_in_generation.get(
                paper_id, False
            ):
                # 本轮还没跑完这份材料。旧 run 仍可审计查看，但不能冒充
                # 新一轮的结果。
                continue
            latest = _latest_run(by_paper.get(paper_id, []))
            if latest is not None:
                runs[paper_id] = latest.id
                run_statuses[paper_id] = latest.status

    pairs = tuple((paper_id, runs.get(paper_id)) for paper_id in paper_ids)
    return ResultSelection(
        batch_id=batch.id,
        generation=generation,
        paper_ids=paper_ids,
        runs=runs,
        run_statuses=run_statuses,
        revision=_compute_revision(batch.id, generation, pairs),
    )


__all__ = [
    "ResultSelection",
    "select_current_results",
    "current_job",
    "SCORED_STATUSES",
]
