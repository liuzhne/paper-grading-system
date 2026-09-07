"""浏览器验收的合成数据（前端 v2 计划 §12.1）。

全部为人工编造的示例内容，**不含真实论文、学生身份或教师成绩**。因此截图与
trace 可以安全归档。

场景覆盖各页断言所需的边界：
- 一个含待确认项与阻塞任务的批次（复核页、导出预检）
- 一个证据引文已失配的评分项（工作区的 quote_not_found 路径）
- 一个没有 AI 分的 Core blocked 项（置信度「未提供」、不可批量采纳）
- 一份没有扣分细则的评分项（评分标准页的阻断项）
"""

from backend.app.db.models import AtomicRule
from backend.app.db.models import GradingBatch
from backend.app.db.models import ManualReviewTask
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricCriterion
from backend.app.db.models import RubricVersion
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.db.models import User
from backend.app.db.session import SessionLocal


LOW_CONFIDENCE = [
    {
        "source": "deterministic",
        "code": "low_confidence",
        "message": "模型置信度 0.61 低于阈值 0.65。",
        "rule_code": None,
    }
]
NO_CONFIDENCE = [
    {
        "source": "deterministic",
        "code": "confidence_unavailable",
        "message": "该评分路径未提供置信度，无法据此判断把握程度。",
        "rule_code": None,
    }
]

#: 0022_legacy_tenant_backfill 建立的不可变默认组织。
DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000002"

SECTIONS = [
    ("3.1 研究背景", 11, "本研究关注微服务链路追踪中的采样策略问题。现有方案在高吞吐场景下会丢失关键链路。"),
    ("3.2 研究方法", 12, "本文采用分层抽样方法，按服务重要度划分四层，样本量为 240 条链路。"),
    ("4.1 实验设计", 13, "实验在包含 32 个服务的测试集群上进行，对比三种基线采样器。"),
]


def seed_all():
    with SessionLocal() as session:
        user = User(
            username="e2e-seed",
            email="e2e@example.invalid",
            display_name="验收种子",
            password_hash="not-a-real-hash",
        )
        session.add(user)
        session.flush()

        rubric = _seed_rubric(session, user)
        _seed_batch_with_review_work(session, rubric)
        _seed_empty_draft_batch(session, rubric)
        session.commit()


def _seed_rubric(session, user):
    rubric = Rubric(
        name="本科毕业论文评分标准",
        version="v3.1",
        total_score=100,
        status="draft",
        visibility="organization",
    )
    session.add(rubric)
    session.flush()

    version_hash = "e" * 64
    compilation = RubricCompilation(
        rubric_id=rubric.id,
        status="validated",
        parser_version="e2e-parser@1",
        compiler_version="e2e-compiler@1",
        prompt_version="e2e-prompt@1",
        created_by=user.id,
        # 复合外键要求 version_hash 与 final_version_hash 一致；缺了它
        # 真实 schema 会以 FOREIGN KEY constraint failed 拒绝插入。
        final_version_hash=version_hash,
    )
    session.add(compilation)
    session.flush()
    version = RubricVersion(
        rubric_id=rubric.id,
        compilation_id=compilation.id,
        version="v3.1",
        workflow_profile="thesis",
        version_hash=version_hash,
        created_by=user.id,
    )
    session.add(version)
    session.flush()

    # T02 故意不给规则：评分标准页要显示阻断项。
    spec = [
        ("T01", "选题与意义", 10, [("compiler", "approved")] * 3),
        ("T02", "文献综述", 15, []),
        ("T03", "研究方法与技术方案", 25, [("compiler", "approved"), ("llm", "draft")]),
    ]
    for code, name, max_score, rules in spec:
        criterion = RubricCriterion(
            rubric_id=rubric.id, code=code, name=name, max_score=max_score, weight=1
        )
        session.add(criterion)
        session.flush()
        for index, (method, status) in enumerate(rules):
            session.add(
                AtomicRule(
                    rubric_version_id=version.id,
                    criterion_id=criterion.id,
                    rule_code="%s-R%d" % (code, index),
                    name="示例规则",
                    rule_text="示例扣分规则文本",
                    direction="deduct",
                    effect_type="score",
                    judge_type="semantic",
                    strictness="required",
                    applies_to="all",
                    status=status,
                    creation_method=method,
                )
            )
    return rubric


def _criteria(session, rubric):
    return (
        session.query(RubricCriterion)
        .filter(RubricCriterion.rubric_id == rubric.id)
        .order_by(RubricCriterion.code)
        .all()
    )


def _seed_batch_with_review_work(session, rubric):
    batch = GradingBatch(
        name="2026 届毕业论文评分 · 批次 1",
        department="计算机学院",
        major="软件工程",
        rubric_id=rubric.id,
        status="scored_with_errors",
        state_version=1,
    )
    session.add(batch)
    session.flush()

    criteria = _criteria(session, rubric)
    people = [
        ("SE-2026-009", "示例学生甲", "面向微服务的链路追踪采样策略"),
        ("SE-2026-011", "示例学生乙", "基于图神经网络的代码缺陷定位"),
    ]
    for position, (student_id, student_name, title) in enumerate(people):
        paper = Paper(
            batch_id=batch.id,
            file_name="%s.pdf" % student_id,
            file_path="%s.pdf" % student_id,
            status="parsed",
            parse_quality=0.82,
            student_id=student_id,
            student_name=student_name,
            title=title,
        )
        session.add(paper)
        session.flush()

        chunks = []
        for section, page, text in SECTIONS:
            chunk = PaperChunk(
                paper_id=paper.id,
                section_title=section,
                page_start=page,
                page_end=page,
                paragraph_ids=[],
                text=text,
            )
            session.add(chunk)
            session.flush()
            chunks.append(chunk)

        run = ScoringRun(paper_id=paper.id, rubric_id=rubric.id, status="scored")
        session.add(run)
        session.flush()

        # 第一份带完整的三种证据形态；第二份全部已确认。
        if position == 0:
            items = [
                (criteria[0], 8, 0.94, False, None,
                 [{"quote": "本研究关注微服务链路追踪", "location": "3.1", "chunk_id": chunks[0].id}]),
                (criteria[1], 12, 0.61, True, LOW_CONFIDENCE,
                 [{"quote": "本文采用分层抽样方法", "location": "3.2", "chunk_id": chunks[1].id}]),
                # 引文已与当前解析文本失配：工作区应跳到块但不高亮。
                (criteria[2], 20, 0.83, False, None,
                 [{"quote": "这句话已经不在当前解析文本里了", "location": "4.1", "chunk_id": chunks[2].id}]),
            ]
        else:
            items = [
                (criteria[0], 9, 0.92, False, None, []),
                (criteria[1], 13, 0.88, False, None, []),
                (criteria[2], 22, 0.9, False, None, []),
            ]

        for criterion, score, confidence, need_review, reasons, evidence in items:
            session.add(
                ScoreItem(
                    scoring_run_id=run.id,
                    criterion_id=criterion.id,
                    max_score=criterion.max_score,
                    ai_score=score,
                    final_score=score,
                    evidence_sufficient=bool(evidence),
                    reason="示例评分理由",
                    deductions=[],
                    deduction_items=[],
                    evidence=evidence,
                    confidence=confidence,
                    need_manual_review=need_review,
                    review_reasons=reasons,
                    review_reason=(reasons[0]["message"] if reasons else None),
                )
            )

        if position == 0:
            # 无 AI 分的 blocked 项：置信度「未提供」，且不可批量采纳。
            session.add(
                ScoreItem(
                    scoring_run_id=run.id,
                    criterion_id=criteria[1].id,
                    max_score=criteria[1].max_score,
                    ai_score=None,
                    final_score=None,
                    evidence_sufficient=False,
                    reason="规则执行被阻断",
                    deductions=[],
                    deduction_items=[],
                    evidence=[],
                    need_manual_review=True,
                    review_reasons=NO_CONFIDENCE,
                    review_reason=NO_CONFIDENCE[0]["message"],
                    aggregation={"blocked": True},
                    aggregation_schema_version="aggregation@1",
                    auto_score_status="blocked",
                )
            )
            session.add(
                ManualReviewTask(
                    # organization_id 必填；用 0022 回填的默认组织。
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    scoring_run_id=run.id,
                    criterion_code=criteria[1].code,
                    trigger_code="provider_error",
                    trigger_message="示例：Provider 调用失败，该项结论尚不成立。",
                    blocking_final_total=True,
                    status="open",
                )
            )
            session.add(
                SpreadsheetWriteLog(
                    scoring_run_id=run.id, target_type="excel", status="succeeded"
                )
            )


def _seed_empty_draft_batch(session, rubric):
    session.add(
        GradingBatch(
            name="软件工程导论 课程报告",
            department="软件学院",
            major="软工 2024 级",
            rubric_id=rubric.id,
            status="draft",
            state_version=1,
        )
    )


__all__ = ["seed_all"]
