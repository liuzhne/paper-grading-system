"""浏览器验收的合成数据（前端 v2 计划 §12.1）。

全部为人工编造的示例内容，**不含真实论文、学生身份或教师成绩**。因此截图与
trace 可以安全归档。

场景覆盖各页断言所需的边界：
- 一个含待确认项与阻塞任务的批次（复核页、导出预检）
- 一个证据引文已失配的评分项（工作区的 quote_not_found 路径）
- 一个没有 AI 分的 Core blocked 项（置信度「未提供」、不可批量采纳）
- 一份没有扣分细则的评分项（评分标准页的阻断项）
"""

from datetime import datetime

import sqlalchemy as sa

from backend.app.db.models import AtomicRule
from backend.app.db.models import GradingBatch
from backend.app.db.models import ManualReviewTask
from backend.app.db.models import Paper
from backend.app.db.models import Base
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

COHERENCE_FINDINGS = [
    {
        "severity": "warn",
        "kind": "figure_reference",
        "message": "示例：图 3 未在正文中被引用。",
        "deducted_by": None,
    }
]
FORMAT_FINDINGS = [
    {
        "severity": "error",
        "field": "line_spacing",
        "message": "示例：正文行距不是模板规定的 1.5 倍。",
        "deducted_by": "T01",
        "deducted_points": 2,
    },
    {
        "severity": "warn",
        "field": "margin",
        "message": "示例：页边距小于模板规定值。",
        "deducted_by": None,
    },
]

#: 0022_legacy_tenant_backfill 建立的不可变默认组织。
DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000002"

SECTIONS = [
    ("3.1 研究背景", 11, "本研究关注微服务链路追踪中的采样策略问题。现有方案在高吞吐场景下会丢失关键链路。"),
    ("3.2 研究方法", 12, "本文采用分层抽样方法，按服务重要度划分四层，样本量为 240 条链路。"),
    ("4.1 实验设计", 13, "实验在包含 32 个服务的测试集群上进行，对比三种基线采样器。"),
]


def organization_scoped_models():
    """所有带**可空** `organization_id` 的模型。

    从映射注册表推导，不写死清单：写死的那份是按「今天有哪些查询按组织过滤」
    选出来的，哪天有人给别的表加上过滤，它不会自己更新，验收会静默地变成空
    数据——**而空数据往往还是「通过」**，因为断言写的是「看不到别的组织的东西」。

    NOT NULL 的列排除在外：那些在建行时就必须给值，回填轮不到它们；把它们收
    进来只会掩盖「建行时忘了给组织」这个更该暴露的问题。
    """
    models = []
    for mapper in Base.registry.mappers:
        column = mapper.columns.get("organization_id")
        if column is not None and column.nullable:
            models.append(mapper.class_)
    return sorted(models, key=lambda model: model.__name__)


def seed_all(*, with_auth=False, with_platform_model=True):
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
        if with_auth:
            _seed_identities(session, rubric)
            if with_platform_model:
                _seed_platform_llm(session)
        session.commit()


#: 验收账号的口令。**只用于一次性验收环境**：库建在 tmp 下、进程退出即弃，
#: 里面没有任何真实论文或学生信息。生产口令不出现在仓库里。
E2E_PASSWORD = "e2e-Acceptance-1"

SECOND_ORGANIZATION_ID = "00000000-0000-0000-0000-0000000000b2"


def _seed_platform_llm(session):
    """给验收环境配一个平台默认模型（D-028）。

    不配的话，模型守卫会把每个登录用户直接送到「账户与连接」——那是**正确行为**，
    但它会让 V01/V02/V10 全部挂在第一步。这里配上，等于模拟「管理员已经配好了」
    这个正常状态；「未配置时确实被引导」由 `llm-setup.spec.js` 单独验证。

    这个 key 是假的，验收里不会真的外呼：所有评分结果都是种子直接写进去的。
    """
    from backend.app.services import platform_llm

    platform_llm.set_config(
        session,
        provider_type="openai_compatible",
        base_url="https://acceptance.invalid/v1",
        model_name="acceptance-model",
        api_key="sk-acceptance-not-a-real-key",
        configured_by="e2e-platform-admin",
    )


def _seed_identities(session, rubric):
    """两个组织、三种角色，供 V01/V02/V10 使用。

    组织隔离要能被证伪，就必须有**第二个组织**和一份只属于它的数据：只有一个
    组织时，「没串数据」和「根本没有别的数据可串」看起来完全一样。
    """
    from backend.app.db.models import Organization
    from backend.app.db.models import OrganizationMember
    from backend.app.services.auth import hash_password

    first = session.get(Organization, DEFAULT_ORGANIZATION_ID)
    if first is None:
        first = Organization(id=DEFAULT_ORGANIZATION_ID, name="示例大学 计算机学院")
        session.add(first)
    second = Organization(id=SECOND_ORGANIZATION_ID, name="示例大学 外国语学院")
    session.add(second)
    session.flush()

    people = [
        ("teacher@example.invalid", "王教师", "user", [(first.id, "teacher")]),
        (
            "orgadmin@example.invalid",
            "李管理员",
            "user",
            [(first.id, "org_admin")],
        ),
        (
            "platform@example.invalid",
            "平台管理员",
            "platform_admin",
            [(first.id, "org_admin"), (second.id, "org_admin")],
        ),
    ]
    for email, display_name, platform_role, memberships in people:
        account = User(
            username=email.split("@")[0],
            email=email,
            display_name=display_name,
            password_hash=hash_password(E2E_PASSWORD),
            platform_role=platform_role,
            email_verified_at=datetime.utcnow(),
        )
        session.add(account)
        session.flush()
        for organization_id, role in memberships:
            session.add(
                OrganizationMember(
                    organization_id=organization_id,
                    user_id=account.id,
                    role=role,
                )
            )

    # 无鉴权模式下 principal.organization_id 为 None，组织过滤不生效，所以既有
    # 种子从不设 organization_id。开鉴权之后过滤真的生效，这些行必须归属到默认
    # 组织，否则登录进来看到的是一个空系统——那会被误读成「数据没种上」。
    for model in organization_scoped_models():
        for row in session.scalars(sa.select(model)).all():
            if row.organization_id is None:
                row.organization_id = DEFAULT_ORGANIZATION_ID
                session.add(row)

    # 只属于第二个组织的批次：切换组织后它必须出现，切回来必须消失。
    other = GradingBatch(
        name="外国语学院 翻译实践报告",
        rubric_id=rubric.id,
        status="draft",
        organization_id=SECOND_ORGANIZATION_ID,
    )
    session.add(other)


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

        run = ScoringRun(
            paper_id=paper.id,
            rubric_id=rubric.id,
            status="scored",
            # 篇章与格式发现：第一份带，第二份干净。工作区中栏的两个页签读它们，
            # 两种状态都要有，否则「没有发现时说什么」测不到。
            coherence_findings=(COHERENCE_FINDINGS if position == 0 else []),
            format_findings=(FORMAT_FINDINGS if position == 0 else []),
        )
        session.add(run)
        session.flush()

        # 第一份带完整的三种证据形态；第二份只留一项待确认。
        #
        # 第二份必须留下**一条可采纳项**：复核页的「逐项确认」与「批量采纳」都要
        # 消掉一条，而验收共用同一个后端、按声明顺序跑。只留一条时，先跑的那个
        # 用例把它吃掉，后跑的那个面对空队列——失败原因看起来像功能坏了，实际是
        # 两个用例在抢同一行数据。
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
                (criteria[2], 22, 0.62, True, LOW_CONFIDENCE, []),
            ]

        # 总分按逐项求和回填：写死一个数会和右栏的评分表对不上，而工作台的
        # 分布图读的正是 run 上的总分。
        run.ai_total_score = sum(item[1] for item in items)
        run.final_total_score = run.ai_total_score
        session.add(run)

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
