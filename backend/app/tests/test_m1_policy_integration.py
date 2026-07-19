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
        "scoring_mode": "llm_direct",
        "evidence_hints": ["研究", "结论"],
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

    # 无 provenance 的旧 Rubric 只有在显式发布后才可作为 legacy_unversioned
    # 权威输入；先发布再建立批次，避免测试绕过真实纵向入口。
    publish_response = client.post(f"/api/rubrics/{rubric['id']}/publish")
    assert publish_response.status_code == 200, publish_response.text
    rubric = publish_response.json()
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


def _item_contribution(item) -> Decimal | None:
    # API 可额外投影 contribution，但 0012 的权威持久化位置是 aggregation JSON。
    projected = _field(item, "contribution", default=None)
    if projected is not None:
        return _decimal(projected)
    aggregation = _field(item, "aggregation")
    value = _field(aggregation, "contribution")
    return None if value is None else _decimal(value)


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
def test_rubric_create_and_update_accept_weight_scale_independent_configuration(client):
    _require_policy_api()
    create_response = client.post(
        "/api/rubrics",
        json=_rubric_payload("M1-weight-scale", weights=(8, 2)),
    )
    assert create_response.status_code == 200, create_response.text
    rubric = create_response.json()
    assert [row["weight"] for row in rubric["criteria"]] == [8, 2]

    # 更新走与 create 相同的验证器；权重无需凑成 100 或 total_score。
    update_response = client.patch(
        f"/api/rubrics/{rubric['id']}",
        json={
            "total_score": 100,
            "criteria": [
                _criterion("C1", weight=80),
                _criterion("C2", weight=20),
            ],
        },
    )
    assert update_response.status_code == 200, update_response.text
    assert [row["weight"] for row in update_response.json()["criteria"]] == [80, 20]

    points_response = client.patch(
        f"/api/rubrics/{rubric['id']}",
        json={
            "total_score": 20,
            "criteria": [
                _criterion("C1", weight=None),
                _criterion("C2", weight=None),
            ],
        },
    )
    assert points_response.status_code == 200, points_response.text
    assert all(row["weight"] is None for row in points_response.json()["criteria"])


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
    _force_mixed_weights(client, rubric_id)

    publish_response = client.post(f"/api/rubrics/{rubric_id}/publish")
    _assert_error_mentions_weight(publish_response)


@requires_policy_api
def test_scoring_start_revalidates_weights_and_creates_no_partial_run(client):
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

    publish_response = client.post(f"/api/rubrics/{rubric_id}/publish")
    assert publish_response.status_code == 200, publish_response.text
    assert publish_response.json()["status"] == "published"

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

    _force_mixed_weights(client, rubric_id)
    score_response = client.post(f"/api/papers/{paper_id}/score")
    _assert_error_mentions_weight(score_response)
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
    assert all(item["aggregation_schema_version"] == "score-item-aggregation@1" for item in items)
    assert all(_item_contribution(item) is not None for item in items)
    assert all(
        {"raw", "max", "weight", "contribution"}.issubset(item["aggregation"])
        for item in items
    )
    assert sum(_item_contribution(item) for item in items) == _decimal(run["ai_total_score"])


@requires_policy_persistence
def test_cross_request_item_review_uses_frozen_policy_after_rubric_weights_drift(client):
    _require_policy_persistence()
    run, items, _paper_id = _create_scored_weighted_run(client, name="M1-frozen-review")
    frozen_hash = run["policy_hash"]

    # 模拟评分后可变 ORM 标准发生漂移。若人工改单项/submit_review 错误地
    # 重读当前 20/80 权重，下面两个分数会聚合为 90；冻结的 80/20 口径应为 60。
    _force_weights(client, run["rubric_id"], (20, 80))

    by_code = {item["criterion_code"]: item for item in items}
    first_override = client.patch(
        f"/api/score-items/{by_code['C1']['id']}",
        json={"final_score": 5, "reason": "普通人工改单项"},
    )
    assert first_override.status_code == 200, first_override.text
    assert _item_contribution(first_override.json()) == Decimal("40")

    second_override = client.patch(
        f"/api/score-items/{by_code['C2']['id']}",
        json={"final_score": 10, "reason": "普通人工改单项"},
    )
    assert second_override.status_code == 200, second_override.text
    assert _item_contribution(second_override.json()) == Decimal("20")

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
def test_published_legacy_non_100_full_score_uses_percentage_grade_scale(client):
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
        assert _item_contribution(override_response.json()) == Decimal("10")

    review_response = client.post(
        f"/api/scoring-runs/{run['id']}/review",
        json={"reason": "确认 20 分制满分按百分比等级解释"},
    )
    assert review_response.status_code == 200, review_response.text
    reviewed = review_response.json()
    assert _decimal(reviewed["final_total_score"]) == Decimal("20")
    assert reviewed["grade"] == "优秀"


@requires_policy_identity
def test_item_override_and_submit_review_use_frozen_half_up_rounding(client):
    _require_policy_identity()
    run, items, _paper_id = _create_scored_run(
        client,
        name="M1-frozen-half-up",
        weights=(None, None),
        total_score=20,
    )

    # 该运行代表“创建时即冻结为 half_up / 1 位”的合法历史事实。直接写入
    # 测试数据库只用于构造不同于部署默认值的冻结策略；后续 API 请求不得重读默认值。
    snapshot = deepcopy(run["policy_snapshot"])
    snapshot["rounding"] = {**snapshot["rounding"], "mode": "half_up", "digits": 1}
    snapshot_without_hash = deepcopy(snapshot)
    snapshot_without_hash.pop("policy_hash", None)
    frozen_hash = _canonical_sha256(snapshot_without_hash)
    if "policy_hash" in snapshot:
        snapshot["policy_hash"] = frozen_hash

    db_generator, db = _db_session_from_client_fixture()
    try:
        db.execute(
            update(ScoringRun)
            .where(ScoringRun.id == run["id"])
            .values(
                policy_snapshot=snapshot,
                policy_hash=frozen_hash,
                policy_schema_version=snapshot["schema_version"],
            )
        )
        db.commit()
    finally:
        db.close()
        db_generator.close()

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
    assert _decimal(after_override["final_total_score"]) == Decimal("2.3")
    assert after_override["policy_hash"] == frozen_hash

    reviewed_response = client.post(
        f"/api/scoring-runs/{run['id']}/review",
        json={"reason": "确认冻结 half-up 舍入口径"},
    )
    assert reviewed_response.status_code == 200, reviewed_response.text
    reviewed = reviewed_response.json()
    assert _decimal(reviewed["final_total_score"]) == Decimal("2.3")
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
def test_historical_run_without_policy_snapshot_cannot_recalculate_across_requests(client):
    _require_policy_persistence()
    run, items, _paper_id = _create_scored_weighted_run(client, name="M1-legacy-null-policy")

    db_generator, db = _db_session_from_client_fixture()
    try:
        historical = db.get(ScoringRun, run["id"])
        historical.policy_snapshot = None
        historical.policy_hash = None
        historical.policy_schema_version = None
        db.commit()
    finally:
        db.close()
        db_generator.close()

    # 普通改单项会触发跨请求聚合；没有冻结口径时必须 fail closed，不能采用
    # 此刻部署的 corrected_thesis_policy 重写历史事实。
    override_response = client.patch(
        f"/api/score-items/{items[0]['id']}",
        json={"final_score": 5, "reason": "历史 run 不得按当前 policy 重算"},
    )
    assert override_response.status_code in {400, 409, 422}, override_response.text
    assert "policy" in override_response.text.lower(), override_response.text

    historical_after = client.get(f"/api/scoring-runs/{run['id']}")
    assert historical_after.status_code == 200, historical_after.text
    assert historical_after.json()["policy_snapshot"] is None
    assert historical_after.json()["policy_hash"] is None

    # 显式 retry 创建新的 rescore generation；新 run 必须拥有 corrected policy，
    # 原历史 run 仍保持空快照，不做伪回填。
    retry_response = client.post(f"/api/scoring-runs/{run['id']}/retry")
    assert retry_response.status_code == 200, retry_response.text
    retried = retry_response.json()
    assert retried["id"] != run["id"]
    assert _policy_key(retried["policy_snapshot"]) == "corrected_thesis_policy"
    _assert_sha256(retried["policy_hash"])
    assert client.get(f"/api/scoring-runs/{run['id']}").json()["policy_snapshot"] is None


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
        aggregation.update(
            {
                "raw": None,
                "max": str(stored_item.max_score),
                "weight": aggregation.get("weight"),
                "contribution": None,
            }
        )
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
