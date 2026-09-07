"""扣分细则完整度（前端 v2 计划 §6）。

评分标准页要回答两个问题：

1. **每个评分项有没有可执行的扣分规则？** 没有的是阻断项——发布后评分到
   这一项时没有判据可用，只能靠模型自由发挥，那正是「扣哪项、扣几分来自
   用户授权的模板」想要防住的事。
2. **规则从哪来？** 用户原文编译（``compiler`` / ``manual`` / 历史升级）与
   AI 起草（``llm``）分开计数。未经确认的 AI 规则不该被当成用户已认可的判据，
   界面上必须能一眼看出哪些还等着人确认。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.db.models import AtomicRule
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricCriterion
from backend.app.db.models import RubricVersion


#: 视为「用户提供」的规则来源。legacy_upgrade 是历史数据的原样搬迁，
#: 同样源自用户当初的输入，不是模型生成的。
USER_SOURCED_METHODS = frozenset({"compiler", "manual", "legacy_upgrade"})

#: 已通过审核、可进入可执行版本的规则状态。
APPROVED_STATUS = "approved"


def _latest_version_id(session, rubric_id):
    return session.scalar(
        select(RubricVersion.id)
        .join(RubricCompilation, RubricCompilation.id == RubricVersion.compilation_id)
        .where(RubricVersion.rubric_id == rubric_id)
        .order_by(RubricCompilation.created_at.desc(), RubricVersion.id.desc())
        .limit(1)
    )


def build_rule_coverage(session, rubric):
    criteria = session.scalars(
        select(RubricCriterion)
        .where(RubricCriterion.rubric_id == rubric.id)
        .order_by(RubricCriterion.code)
    ).all()

    version_id = _latest_version_id(session, rubric.id)
    rules_by_criterion = {}
    if version_id:
        for rule in session.scalars(
            select(AtomicRule).where(AtomicRule.rubric_version_id == version_id)
        ).all():
            rules_by_criterion.setdefault(rule.criterion_id, []).append(rule)

    entries = []
    complete = blocking = pending = 0
    for criterion in criteria:
        rules = rules_by_criterion.get(criterion.id, [])
        approved = [r for r in rules if r.status == APPROVED_STATUS]
        ai_pending = [
            r
            for r in rules
            if r.creation_method not in USER_SOURCED_METHODS
            and r.status != APPROVED_STATUS
        ]
        user_pending = [
            r
            for r in rules
            if r.creation_method in USER_SOURCED_METHODS
            and r.status != APPROVED_STATUS
        ]

        if not rules:
            status = "missing"
            blocking += 1
        elif ai_pending or user_pending:
            status = "pending_review"
            pending += 1
        else:
            status = "complete"
            complete += 1

        entries.append(
            {
                "criterion_id": criterion.id,
                "code": criterion.code,
                "name": criterion.name,
                "max_score": float(criterion.max_score),
                "status": status,
                "rule_count": len(rules),
                "approved_count": len(approved),
                "ai_pending_count": len(ai_pending),
                "user_pending_count": len(user_pending),
                "from_source_count": sum(
                    1 for r in rules if r.creation_method in USER_SOURCED_METHODS
                ),
                "from_ai_count": sum(
                    1 for r in rules if r.creation_method not in USER_SOURCED_METHODS
                ),
            }
        )

    return {
        "rubric_id": rubric.id,
        "rubric_version_id": version_id,
        "total_criteria": len(criteria),
        "complete_count": complete,
        "pending_review_count": pending,
        "blocking_count": blocking,
        "criteria": entries,
    }


__all__ = ["build_rule_coverage", "USER_SOURCED_METHODS"]
