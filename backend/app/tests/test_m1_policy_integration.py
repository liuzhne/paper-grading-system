"""M1 policy 在 legacy API/ORM 边界上的集成合同。

本文件不为测试伪造 M1 行为：缺少 Core policy 或 0012 持久化字段时，测试
仍会启动 ``client`` fixture，再由显式能力断言进入 strict xfail。能力一旦存在，
create/update、初评、跨请求复核和历史 run 必须全部使用同一套冻结 policy。
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from importlib import import_module
import re

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import ReviewLog
from backend.app.db.models import RubricCriterion
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.schemas.scoring import ReviewLogRead
from backend.app.schemas.scoring import ScoreItemRead
from backend.app.schemas.scoring import ScoringRunRead
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.conftest import publish_rubric_via_api
from backend.app.tests.conftest import review_rubric_via_api
from backend.app.tests.test_m1_policy import POLICY_API_AVAILABLE
from backend.app.tests.test_m1_policy import _decimal
from backend.app.tests.test_m1_policy import _field
from backend.app.tests.test_m1_policy import _require_policy_api


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _model_has_columns(model, names: set[str]) -> bool:
    return names.issubset(set(model.__table__.columns.keys()))


def _schema_has_fields(schema, names: set[str]) -> bool:
    return names.issubset(set(schema.model_fields))


M1_PERSISTENCE_AVAILABLE = all(
    (
        _model_has_columns(ScoringRun, {"policy_snapshot", "policy_hash", "policy_schema_version"}),
        _model_has_columns(ScoreItem, {"aggregation", "aggregation_schema_version", "auto_score_status"}),
        _model_has_columns(ReviewLog, {"policy_hash", "resolution_type"}),
        _schema_has_fields(ScoringRunRead, {"policy_snapshot", "policy_hash", "policy_schema_version"}),
        _schema_has_fields(ScoreItemRead, {"aggregation", "aggregation_schema_version", "auto_score_status"}),
        _schema_has_fields(ReviewLogRead, {"policy_hash", "resolution_type"}),
    )
)

CANONICAL_MODULE = "backend.app.services.scoring.core.canonical"
try:
    _canonical_module = import_module(CANONICAL_MODULE)
except ModuleNotFoundError as exc:
    # 目标 Core 包尚未建立可作为 M1 能力缺失；实现内部依赖损坏则必须暴露。
    if not CANONICAL_MODULE.startswith(exc.name or ""):
        raise
    _canonical_module = None

_canonical_sha256 = (
    getattr(_canonical_module, "canonical_sha256", None)
    if _canonical_module is not None
    else None
)
CANONICAL_API_AVAILABLE = callable(_canonical_sha256)

requires_policy_api = pytest.mark.xfail(
    not POLICY_API_AVAILABLE,
    reason="M1 Core policy is not available to the rubric API yet",
    strict=True,
)
requires_policy_persistence = pytest.mark.xfail(
    not (POLICY_API_AVAILABLE and M1_PERSISTENCE_AVAILABLE),
    reason="M1 policy snapshot/aggregation persistence contract is not available yet",
    strict=True,
)
requires_policy_identity = pytest.mark.xfail(
    not (POLICY_API_AVAILABLE and M1_PERSISTENCE_AVAILABLE and CANONICAL_API_AVAILABLE),
    reason="M1 policy persistence requires the Core canonical hash primitive",
    strict=True,
)


def _require_policy_persistence() -> None:
    _require_policy_api()
    assert M1_PERSISTENCE_AVAILABLE, (
        "0012 contract requires ScoringRun.policy_*, ScoreItem aggregation/schema/status "
        "and ReviewLog policy_hash/resolution_type in both ORM and API schemas"
    )


def _require_policy_identity() -> None:
    _require_policy_persistence()
    assert CANONICAL_API_AVAILABLE, (
        f"{CANONICAL_MODULE} must expose canonical_sha256 for policy identity binding"
    )


def _criterion(code: str, *, weight, max_score=10) -> dict:
    return {
        "code": code,
        "name": f"评分项-{code}",
        "max_score": max_score,
        "weight": weight,
        "criterion_type": "deterministic",
        "scoring_mode": "deductive",
        "evidence_hints": ["研究", "结论"],
        "deduction_rules_structured": [
            {
                "match": f"{code} 论述不足",
                "points": max_score,
                "reason": f"{code} 论述不足",
                "checker_key": "thesis.legacy_required_fields.v1",
                "checker_params": {
                    "criterion_code": code,
                    "applies_to": "global",
                },
            }
        ],
    }


def _rubric_payload(name: str, *, weights=(80, 20), total_score=100) -> dict:
    return {
        "name": name,
        "version": "v1.0",
        "total_score": total_score,
        "criteria": [
            _criterion("C1", weight=weights[0]),
            _criterion("C2", weight=weights[1]),
        ],
    }


def _assert_error_mentions_weight(response) -> None:
    assert response.status_code in {400, 422}, response.text
    body = response.text.lower()
    assert "weight" in body or "权重" in body, response.text


def _create_scored_run(
    client,
    *,
    name: str,
    weights=(80, 20),
    total_score=100,
) -> tuple[dict, list[dict], str]:
    rubric_response = client.post(
        "/api/rubrics",
        json=_rubric_payload(name, weights=weights, total_score=total_score),
    )
    assert rubric_response.status_code == 200, rubric_response.text
    rubric = rubric_response.json()

    rubric, _identity = publish_rubric_via_api(client, rubric["id"])
    assert rubric["status"] == "published"

    batch_response = client.post(
        "/api/batches",
        json={"name": f"{name}-批次", "rubric_id": rubric["id"]},
    )
    assert batch_response.status_code == 200, batch_response.text

    upload_response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_response.json()["id"]},
        files={"file": ("m1-policy.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload_response.status_code == 200, upload_response.text
    paper_id = upload_response.json()["id"]

    run_response = client.post(f"/api/papers/{paper_id}/score")
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()
    items_response = client.get(f"/api/scoring-runs/{run['id']}/items")
    assert items_response.status_code == 200, items_response.text
    return run, items_response.json(), paper_id


def _create_scored_weighted_run(client, *, name: str) -> tuple[dict, list[dict], str]:
    return _create_scored_run(client, name=name)


def _policy_key(snapshot) -> str:
    return str(_field(snapshot, "policy_key", "name", "key"))


def _item_contributions(item) -> list[dict]:
    """Read the immutable M4 automatic-score contribution audit."""

    aggregation = _field(item, "aggregation")
    assert _field(item, "aggregation_schema_version") == "criterion-aggregation@2"
    assert _field(aggregation, "schema_version") == "criterion-aggregation@2"
    assert _field(aggregation, "criterion_code") == _field(item, "criterion_code")
    contributions = _field(aggregation, "contributions")
    assert isinstance(contributions, list)
    assert all(
        {
            "criterion_code",
            "rule_code",
            "occurrence_id",
            "kind",
            "amount",
        }.issubset(contribution)
        for contribution in contributions
    )
    return contributions


def _item_auto_score_from_contributions(item) -> Decimal:
    return sum(
        (_decimal(contribution["amount"]) for contribution in _item_contributions(item)),
        Decimal("0"),
    )


def _assert_sha256(value: str) -> None:
    digest = value.removeprefix("sha256:")
    assert re.fullmatch(r"[0-9a-f]{64}", digest), value


def _sha256_digest(value) -> str:
    text = str(value)
    _assert_sha256(text)
    return text.removeprefix("sha256:")


def _db_session_from_client_fixture():
    # conftest.client 已将 get_db 指向本用例的内存 SQLite；复用该 override，
    # 以构造 API 无法直接表达的 invalid/blocked 持久化边界。
    override = app.dependency_overrides[get_db]
    generator = override()
    return generator, next(generator)


def _mixed_weight_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评分规则"
    sheet.append(["编号", "评分项", "分值", "权重", "评分说明"])
    sheet.append(["C1", "方法", 10, 80, "方法合理"])
    sheet.append(["C2", "结论", 10, None, "结论充分"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _force_weights(client, rubric_id: str, weights) -> None:
    db_generator, db = _db_session_from_client_fixture()
    try:
        criteria = db.scalars(
            select(RubricCriterion)
            .where(RubricCriterion.rubric_id == rubric_id)
            .order_by(RubricCriterion.display_order, RubricCriterion.code)
        ).all()
        assert len(criteria) == len(weights)
        for criterion, weight in zip(criteria, weights, strict=True):
            criterion.weight = None if weight is None else Decimal(str(weight))
        db.commit()
    finally:
        db.close()
        db_generator.close()


def _force_mixed_weights(client, rubric_id: str) -> None:
    _force_weights(client, rubric_id, (80, None))


@requires_policy_api
def test_rubric_create_accepts_weight_scale_independent_configuration(client):
    _require_policy_api()
    create_response = client.post(
        "/api/rubrics",
        json=_rubric_payload("M1-weight-scale", weights=(8, 2)),
    )
    assert create_response.status_code == 200, create_response.text
    rubric = create_response.json()
    assert [row["weight"] for row in rubric["criteria"]] == [8, 2]

    # M4 versioned content is immutable; exercise the same accepted weight
    # scales through independent provenance imports instead of whole-rubric
    # PATCH, which is intentionally fail-closed.
    percentage_response = client.post(
        "/api/rubrics",
        json=_rubric_payload("M1-weight-percentage", weights=(80, 20)),
    )
    assert percentage_response.status_code == 200, percentage_response.text
    assert [row["weight"] for row in percentage_response.json()["criteria"]] == [80, 20]

    points_response = client.post(
        "/api/rubrics",
        json=_rubric_payload(
            "M1-weight-points",
            weights=(None, None),
            total_score=20,
        ),
    )
    assert points_response.status_code == 200, points_response.text
    assert all(row["weight"] is None for row in points_response.json()["criteria"])

    immutable_update = client.patch(
        f"/api/rubrics/{rubric['id']}",
        json={"description": "legacy whole-rubric patch"},
    )
    assert immutable_update.status_code == 409, immutable_update.text
    assert immutable_update.json()["detail"]["code"] == "RUBRIC_RECOMPILE_REQUIRED"


@requires_policy_api
@pytest.mark.parametrize(
    "weights",
    [
        (80, None),
        (80, 0),
        (80, -1),
        (80.001, 20),
        (10000, 20),
    ],
    ids=["mixed", "zero", "negative", "extra-precision", "out-of-range"],
)
def test_rubric_create_rejects_invalid_weight_boundaries(client, weights):
    _require_policy_api()
    response = client.post(
        "/api/rubrics",
        json=_rubric_payload(f"M1-invalid-weight-{weights!r}", weights=weights),
    )
    _assert_error_mentions_weight(response)


@requires_policy_api
def test_rubric_update_rejects_mixed_weights_with_the_shared_validator(client):
    _require_policy_api()
    create_response = client.post(
        "/api/rubrics",
        json={
            "name": "M1-update-invalid-weight",
            "version": "v1.0",
            "total_score": 20,
            "criteria": [_criterion("C1", weight=None), _criterion("C2", weight=None)],
        },
    )
    assert create_response.status_code == 200, create_response.text

    response = client.patch(
        f"/api/rubrics/{create_response.json()['id']}",
        json={
            "total_score": 100,
            "criteria": [_criterion("C1", weight=80), _criterion("C2", weight=None)],
        },
    )
    _assert_error_mentions_weight(response)


@requires_policy_api
def test_rubric_import_rejects_mixed_weights_with_the_shared_validator(client):
    _require_policy_api()
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": "M1-import-mixed-weight", "version": "v1.0"},
        files={
            "rules_file": (
                "mixed-weight.xlsx",
                _mixed_weight_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    _assert_error_mentions_weight(response)


@requires_policy_api
def test_publish_revalidates_weight_configuration_instead_of_trusting_create(client):
    _require_policy_api()
    create_response = client.post(
        "/api/rubrics",
        json={
            "name": "M1-publish-revalidation",
            "version": "v1.0",
            "total_score": 20,
            "criteria": [_criterion("C1", weight=None), _criterion("C2", weight=None)],
        },
    )
    assert create_response.status_code == 200, create_response.text
    rubric_id = create_response.json()["id"]
    identity = review_rubric_via_api(client, rubric_id)
    _force_mixed_weights(client, rubric_id)

    publish_response = client.post(
        f"/api/rubrics/{rubric_id}/publish",
        json={
            "compilation_id": identity["compilation_id"],
            "reason": "验证发布阶段重新检查权重",
        },
    )
    _assert_error_mentions_weight(publish_response)


@requires_policy_api
def test_published_version_rejects_projection_drift_before_scoring(client):
    _require_policy_api()
    rubric_response = client.post(
        "/api/rubrics",
        json={
            "name": "M1-score-revalidation",
            "version": "v1.0",
            "total_score": 20,
            "criteria": [_criterion("C1", weight=None), _criterion("C2", weight=None)],
        },
    )
    assert rubric_response.status_code == 200, rubric_response.text
    rubric_id = rubric_response.json()["id"]

    published, _identity = publish_rubric_via_api(client, rubric_id)
    assert published["status"] == "published"

    batch_response = client.post(
        "/api/batches",
        json={"name": "M1-score-revalidation-batch", "rubric_id": rubric_id},
    )
    assert batch_response.status_code == 200, batch_response.text
    upload_response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_response.json()["id"]},
        files={"file": ("m1-score-revalidation.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload_response.status_code == 200, upload_response.text
    paper_id = upload_response.json()["id"]

    with pytest.raises(ValueError, match="已发布评分标准不可修改"):
        _force_mixed_weights(client, rubric_id)
    runs_response = client.get("/api/scoring-runs", params={"paper_id": paper_id})
    assert runs_response.status_code == 200, runs_response.text
    assert runs_response.json() == []


@requires_policy_identity
def test_new_authoritative_run_freezes_corrected_policy_and_item_contributions(client):
    _require_policy_identity()
    run, items, _paper_id = _create_scored_weighted_run(client, name="M1-frozen-new-run")

    snapshot = run["policy_snapshot"]
    assert _policy_key(snapshot) == "corrected_thesis_policy"
    usage = _field(snapshot, "usage", "allowed_usage", default=None)
    if usage is not None:
        assert usage == "authoritative_new_runs"
    assert "legacy_behavior_policy" not in str(snapshot)
    assert run["policy_schema_version"] == _field(snapshot, "schema_version")

    run_policy_digest = _sha256_digest(run["policy_hash"])
    snapshot_policy_hash = _field(snapshot, "policy_hash", default=None)
    if snapshot_policy_hash is not None:
        assert _sha256_digest(snapshot_policy_hash) == run_policy_digest

    canonical_policy_payload = dict(snapshot)
    canonical_policy_payload.pop("policy_hash", None)
    assert _sha256_digest(_canonical_sha256(canonical_policy_payload)) == run_policy_digest

    assert len(items) == 2
    assert {item["auto_score_status"] for item in items} == {"calculated"}
    assert all(
        _item_auto_score_from_contributions(item) == _decimal(item["ai_score"])
        for item in items
    )
    by_code = {item["criterion_code"]: item for item in items}
    expected_total = (
        _decimal(by_code["C1"]["ai_score"])
        / _decimal(by_code["C1"]["max_score"])
        * Decimal("80")
        + _decimal(by_code["C2"]["ai_score"])
        / _decimal(by_code["C2"]["max_score"])
        * Decimal("20")
    )
    assert expected_total == _decimal(run["ai_total_score"])


@requires_policy_persistence
def test_cross_request_item_review_uses_frozen_policy(client):
    _require_policy_persistence()
    run, items, _paper_id = _create_scored_weighted_run(client, name="M1-frozen-review")
    frozen_hash = run["policy_hash"]

    # M4 published graphs are immutable, so cross-request review must consume
    # the policy already frozen on the run rather than re-reading rubric rows.

    by_code = {item["criterion_code"]: item for item in items}
    first_override = client.patch(
        f"/api/score-items/{by_code['C1']['id']}",
        json={"final_score": 5, "reason": "普通人工改单项"},
    )
    assert first_override.status_code == 200, first_override.text
    first_item = first_override.json()
    assert _decimal(first_item["final_score"]) == Decimal("5")
    assert first_item["aggregation"] == by_code["C1"]["aggregation"]
    assert _item_auto_score_from_contributions(first_item) == _decimal(
        by_code["C1"]["ai_score"]
    )

    second_override = client.patch(
        f"/api/score-items/{by_code['C2']['id']}",
        json={"final_score": 10, "reason": "普通人工改单项"},
    )
    assert second_override.status_code == 200, second_override.text
    second_item = second_override.json()
    assert _decimal(second_item["final_score"]) == Decimal("10")
    assert second_item["aggregation"] == by_code["C2"]["aggregation"]
    assert _item_auto_score_from_contributions(second_item) == _decimal(
        by_code["C2"]["ai_score"]
    )

    review_response = client.post(
        f"/api/scoring-runs/{run['id']}/review",
        json={"reason": "按运行时冻结口径完成复核"},
    )
    assert review_response.status_code == 200, review_response.text
    reviewed = review_response.json()
    assert reviewed["policy_hash"] == frozen_hash
    assert reviewed["policy_snapshot"] == run["policy_snapshot"]
    assert _decimal(reviewed["final_total_score"]) == Decimal("60")
    assert reviewed["grade"] == "及格"

    logs_response = client.get(f"/api/scoring-runs/{run['id']}/review-logs")
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()
    assert len(logs) == 3
    assert {log["policy_hash"] for log in logs} == {frozen_hash}
    assert [log["resolution_type"] for log in logs[:2]] == ["ordinary_override", "ordinary_override"]
    assert logs[-1]["resolution_type"] is not None
    assert logs[-1]["resolution_type"] not in {"resolve_validation", "resolve_block", "rescore"}


@requires_policy_persistence
def test_published_version_non_100_full_score_uses_percentage_grade_scale(client):
    _require_policy_persistence()
    run, items, _paper_id = _create_scored_run(
        client,
        name="M1-published-legacy-non-100",
        weights=(None, None),
        total_score=20,
    )

    assert _policy_key(run["policy_snapshot"]) == "corrected_thesis_policy"
    assert len(items) == 2
    for item in items:
        override_response = client.patch(
            f"/api/score-items/{item['id']}",
            json={"final_score": 10, "reason": "非百分制满分纵向复核"},
        )
        assert override_response.status_code == 200, override_response.text
        overridden = override_response.json()
        assert _decimal(overridden["final_score"]) == Decimal("10")
        assert overridden["aggregation"] == item["aggregation"]
        assert _item_auto_score_from_contributions(overridden) == _decimal(
            item["ai_score"]
        )

    review_response = client.post(
        f"/api/scoring-runs/{run['id']}/review",
        json={"reason": "确认 20 分制满分按百分比等级解释"},
    )
    assert review_response.status_code == 200, review_response.text
    reviewed = review_response.json()
    assert _decimal(reviewed["final_total_score"]) == Decimal("20")
    assert reviewed["grade"] == "优秀"


@requires_policy_identity
def test_item_override_and_submit_review_use_frozen_rounding(client):
    _require_policy_identity()
    run, items, _paper_id = _create_scored_run(
        client,
        name="M1-frozen-half-up",
        weights=(None, None),
        total_score=20,
    )

    frozen_hash = run["policy_hash"]

    by_code = {item["criterion_code"]: item for item in items}
    first = client.patch(
        f"/api/score-items/{by_code['C1']['id']}",
        json={"final_score": 2.25, "reason": "构造 half-up 边界"},
    )
    assert first.status_code == 200, first.text
    second = client.patch(
        f"/api/score-items/{by_code['C2']['id']}",
        json={"final_score": 0, "reason": "其余贡献归零"},
    )
    assert second.status_code == 200, second.text

    after_override_response = client.get(f"/api/scoring-runs/{run['id']}")
    assert after_override_response.status_code == 200, after_override_response.text
    after_override = after_override_response.json()
    assert _decimal(after_override["final_total_score"]) == Decimal("2.25")
    assert after_override["policy_hash"] == frozen_hash

    reviewed_response = client.post(
        f"/api/scoring-runs/{run['id']}/review",
        json={"reason": "确认冻结 half-up 舍入口径"},
    )
    assert reviewed_response.status_code == 200, reviewed_response.text
    reviewed = reviewed_response.json()
    assert _decimal(reviewed["final_total_score"]) == Decimal("2.25")
    assert reviewed["policy_hash"] == frozen_hash

    logs_response = client.get(f"/api/scoring-runs/{run['id']}/review-logs")
    assert logs_response.status_code == 200, logs_response.text
    assert {log["policy_hash"] for log in logs_response.json()} == {frozen_hash}


@requires_policy_persistence
def test_new_retry_run_does_not_rewrite_historical_run_policy_or_scores(client):
    _require_policy_persistence()
    first, _items, _paper_id = _create_scored_weighted_run(client, name="M1-history-preservation")
    first_before = client.get(f"/api/scoring-runs/{first['id']}").json()

    retry_response = client.post(f"/api/scoring-runs/{first['id']}/retry")
    assert retry_response.status_code == 200, retry_response.text
    second = retry_response.json()
    assert second["id"] != first["id"]
    assert _policy_key(second["policy_snapshot"]) == "corrected_thesis_policy"
    _assert_sha256(second["policy_hash"])

    second_items = client.get(f"/api/scoring-runs/{second['id']}/items").json()
    changed = client.patch(
        f"/api/score-items/{second_items[0]['id']}",
        json={"final_score": 0, "reason": "仅调整新一代 run"},
    )
    assert changed.status_code == 200, changed.text

    first_after = client.get(f"/api/scoring-runs/{first['id']}").json()
    assert first_after == first_before


@requires_policy_persistence
def test_core_run_replay_identity_prevents_erasing_frozen_policy(client):
    _require_policy_persistence()
    run, items, _paper_id = _create_scored_weighted_run(client, name="M1-legacy-null-policy")

    db_generator, db = _db_session_from_client_fixture()
    try:
        historical = db.get(ScoringRun, run["id"])
        historical.policy_snapshot = None
        historical.policy_hash = None
        historical.policy_schema_version = None
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()
        db_generator.close()

    historical_after = client.get(f"/api/scoring-runs/{run['id']}")
    assert historical_after.status_code == 200, historical_after.text
    assert historical_after.json()["policy_snapshot"] == run["policy_snapshot"]
    assert historical_after.json()["policy_hash"] == run["policy_hash"]


@requires_policy_persistence
@pytest.mark.parametrize("status", ["invalid", "blocked"])
def test_ordinary_override_cannot_clear_persisted_invalid_or_blocked_status(client, status):
    _require_policy_persistence()
    run, items, _paper_id = _create_scored_weighted_run(client, name=f"M1-{status}-override")
    target = items[0]

    db_generator, db = _db_session_from_client_fixture()
    try:
        stored_item = db.get(ScoreItem, target["id"])
        stored_run = db.get(ScoringRun, run["id"])
        stored_item.auto_score_status = status
        stored_item.ai_score = None
        stored_item.final_score = None
        aggregation = dict(stored_item.aggregation or {})
        aggregation["status"] = status
        aggregation["contributions"] = []
        stored_item.aggregation = aggregation
        stored_item.need_manual_review = True
        stored_run.ai_total_score = None
        stored_run.final_total_score = None
        stored_run.need_manual_review = True
        db.commit()
    finally:
        db.close()
        db_generator.close()

    override_response = client.patch(
        f"/api/score-items/{target['id']}",
        json={"final_score": 10, "reason": "普通 override 不能充当授权 resolution"},
    )
    assert override_response.status_code in {200, 400, 409, 422}, override_response.text
    if override_response.status_code == 200:
        overridden = override_response.json()
        assert overridden["auto_score_status"] == status
        assert _decimal(overridden["final_score"]) == Decimal("10")

    run_after_override = client.get(f"/api/scoring-runs/{run['id']}").json()
    assert run_after_override["final_total_score"] is None
    assert run_after_override["need_manual_review"] is True

    submit_response = client.post(
        f"/api/scoring-runs/{run['id']}/review",
        json={"reason": "尝试普通提交"},
    )
    assert submit_response.status_code in {200, 400, 409, 422}, submit_response.text
    if submit_response.status_code == 200:
        submitted = submit_response.json()
        assert submitted["final_total_score"] is None
        assert submitted["need_manual_review"] is True
        assert submitted["status"] != "reviewed"

    item_after_submit = client.get(f"/api/scoring-runs/{run['id']}/items").json()[0]
    assert item_after_submit["auto_score_status"] == status
    final_run = client.get(f"/api/scoring-runs/{run['id']}").json()
    assert final_run["final_total_score"] is None
