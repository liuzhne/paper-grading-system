"""`GET /api/rubrics/{id}/rule-coverage`（前端 v2 计划 §6）。

评分标准页的「扣分细则完整度」。回答两个问题：

1. 每个评分项有没有可执行的扣分规则？没有的是**阻断项**——发布后评分时
   这一项没有判据可用。
2. 规则从哪来？用户原文编译（compiler / manual）与 AI 起草（llm）必须
   分开计数：未经确认的 AI 规则不该被当成用户已认可的判据。
"""

from backend.app.db import models


def _seed(client, *, criteria):
    """criteria: [(code, [(creation_method, status), ...])]"""
    with client.session_factory() as session:
        user = models.User(
            username="cov-user",
            email="cov@example.test",
            display_name="覆盖度测试用户",
            password_hash="x",
        )
        session.add(user)
        session.flush()
        rubric = models.Rubric(name="cov rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        compilation = models.RubricCompilation(
            rubric_id=rubric.id,
            status="validated",
            parser_version="p1",
            compiler_version="c1",
            prompt_version="pv1",
            created_by=user.id,
        )
        session.add(compilation)
        session.flush()
        version = models.RubricVersion(
            rubric_id=rubric.id,
            compilation_id=compilation.id,
            version="v1",
            workflow_profile="thesis",
            version_hash="c" * 64,
            created_by=user.id,
        )
        session.add(version)
        session.flush()
        for code, rules in criteria:
            criterion = models.RubricCriterion(
                rubric_id=rubric.id, code=code, name=code, max_score=25, weight=1
            )
            session.add(criterion)
            session.flush()
            for index, (method, status) in enumerate(rules):
                session.add(
                    models.AtomicRule(
                        rubric_version_id=version.id,
                        criterion_id=criterion.id,
                        rule_code="%s-R%d" % (code, index),
                        name="rule",
                        rule_text="t",
                        direction="deduct",
                        effect_type="score",
                        judge_type="semantic",
                        strictness="required",
                        applies_to="all",
                        status=status,
                        creation_method=method,
                    )
                )
        session.commit()
        return rubric.id


def test_criterion_with_approved_rules_is_complete(client):
    rubric_id = _seed(client, criteria=[("C01", [("compiler", "approved")])])

    body = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()

    assert body["complete_count"] == 1
    assert body["blocking_count"] == 0
    assert body["criteria"][0]["status"] == "complete"


def test_criterion_without_any_rule_is_blocking(client):
    """没有扣分规则的评分项在评分时没有判据可用。"""
    rubric_id = _seed(client, criteria=[("C01", [])])

    body = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()

    assert body["blocking_count"] == 1
    entry = body["criteria"][0]
    assert entry["status"] == "missing"
    assert entry["rule_count"] == 0


def test_ai_drafted_rules_pending_review_do_not_count_as_complete(client):
    """未确认的 AI 规则不是用户认可的判据，不能算完整。"""
    rubric_id = _seed(client, criteria=[("C01", [("llm", "draft")])])

    body = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()

    entry = body["criteria"][0]
    assert entry["status"] == "pending_review"
    assert entry["ai_pending_count"] == 1
    assert body["complete_count"] == 0


def test_sources_are_counted_separately(client):
    rubric_id = _seed(
        client,
        criteria=[
            ("C01", [("compiler", "approved"), ("llm", "approved"), ("manual", "approved")])
        ],
    )

    entry = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()["criteria"][0]

    assert entry["from_source_count"] == 2, "compiler 与 manual 都来自用户输入"
    assert entry["from_ai_count"] == 1


def test_partially_reviewed_criterion_is_reported_as_pending(client):
    rubric_id = _seed(
        client, criteria=[("C01", [("compiler", "approved"), ("llm", "draft")])]
    )

    entry = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()["criteria"][0]

    assert entry["status"] == "pending_review"
    assert entry["approved_count"] == 1
    assert entry["ai_pending_count"] == 1


def test_summary_aggregates_across_criteria(client):
    rubric_id = _seed(
        client,
        criteria=[
            ("C01", [("compiler", "approved")]),
            ("C02", []),
            ("C03", [("llm", "draft")]),
        ],
    )

    body = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()

    assert body["total_criteria"] == 3
    assert body["complete_count"] == 1
    assert body["blocking_count"] == 1
    assert body["pending_review_count"] == 1


def test_criteria_are_ordered_by_code(client):
    rubric_id = _seed(
        client,
        criteria=[("C03", []), ("C01", []), ("C02", [])],
    )

    codes = [c["code"] for c in client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()["criteria"]]

    assert codes == ["C01", "C02", "C03"]


def test_rubric_without_a_version_reports_all_criteria_missing(client):
    """还没编译出版本时，所有评分项都没有可执行规则。"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="bare", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        session.add(
            models.RubricCriterion(
                rubric_id=rubric.id, code="C01", name="c", max_score=25, weight=1
            )
        )
        session.commit()
        rubric_id = rubric.id

    body = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()

    assert body["blocking_count"] == 1


def test_unknown_rubric_is_404(client):
    assert client.get("/api/rubrics/nope/rule-coverage").status_code == 404
