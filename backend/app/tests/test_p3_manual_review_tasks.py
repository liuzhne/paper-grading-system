"""P2/P3 integration contract for rule checkpoints and human resolution."""

from sqlalchemy import select

from backend.app.db import models
from backend.app.tests.test_m7_technical_proposal_e2e import _create_batch
from backend.app.tests.test_m7_technical_proposal_e2e import _publish_fixture
from backend.app.tests.test_m7_technical_proposal_e2e import _score
from backend.app.tests.test_m7_technical_proposal_e2e import _upload
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.errors import project_provider_error
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.submissions import lifecycle
import httpx


def _completed_run(client):
    _rubric_id, version_id = _publish_fixture(client)
    batch = _create_batch(client, version_id)
    submission, _metadata, _ = _upload(client, batch["id"], variant="complete")
    return _score(client, submission["id"])


def test_persisted_core_run_exposes_one_checkpoint_per_atomic_rule(client):
    run = _completed_run(client)

    response = client.get(f"/api/v2/scoring-runs/{run['id']}/rule-tasks")
    assert response.status_code == 200, response.text
    tasks = response.json()
    assert tasks
    assert len({item["rule_code"] for item in tasks}) == len(tasks)
    assert {item["status"] for item in tasks} <= {
        "succeeded",
        "skipped",
        "review_required",
    }
    assert all(item["attempt_count"] == 1 for item in tasks)


def test_blocking_manual_task_is_claimed_and_resolved_with_evidence(client):
    run = _completed_run(client)
    with client.session_factory() as db:
        rule_task = db.scalar(
            select(models.RuleScoringTask).where(
                models.RuleScoringTask.scoring_run_id == run["id"]
            )
        )
        item = db.get(models.ScoreItem, rule_task.score_item_id)
        rule_task.status = "failed_exhausted"
        rule_task.blocking_final_total = True
        item.auto_score_status = "invalid"
        item.ai_score = None
        item.final_score = None
        item.need_manual_review = True
        item.scoring_run.ai_total_score = None
        item.scoring_run.final_total_score = None
        item.scoring_run.grade = None
        item.scoring_run.need_manual_review = True
        item.scoring_run.status = "pending_review"
        task = models.ManualReviewTask(
            organization_id=item.scoring_run.organization_id,
            scoring_run_id=run["id"],
            rule_scoring_task_id=rule_task.id,
            score_item_id=item.id,
            criterion_code=rule_task.criterion_code,
            rule_code=rule_task.rule_code,
            trigger_code="PROVIDER_UNAVAILABLE",
            trigger_message="semantic provider request failed",
            blocking_final_total=True,
            status="open",
            priority=50,
        )
        db.add(task)
        db.commit()
        task_id = task.id
        item_id = item.id
        source_unit = item.scoring_run.document_snapshot.snapshot_payload[
            "evidence_units"
        ][0]
        evidence_id = source_unit["evidence_unit_id"]
        evidence_quote = source_unit["normalized_text"]

    queue = client.get("/api/v2/manual-review-tasks")
    assert queue.status_code == 200, queue.text
    assert task_id in {entry["id"] for entry in queue.json()}

    detail = client.get(f"/api/v2/manual-review-tasks/{task_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["trigger_code"] == "PROVIDER_UNAVAILABLE"

    claimed = client.post(
        f"/api/v2/manual-review-tasks/{task_id}/claim",
        json={"version": 1},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"

    stale_claim = client.post(
        f"/api/v2/manual-review-tasks/{task_id}/claim",
        json={"version": 1},
    )
    assert stale_claim.status_code == 409, stale_claim.text

    forged_evidence = client.post(
        f"/api/v2/manual-review-tasks/{task_id}/resolve",
        json={
            "version": 2,
            "final_score": 10,
            "reason": "不应接受不属于快照的证据",
            "evidence": [
                {
                    "evidence_unit_id": evidence_id,
                    "quote": "这段文本并不存在于冻结文档快照中",
                }
            ],
        },
    )
    assert forged_evidence.status_code == 422, forged_evidence.text

    resolved = client.post(
        f"/api/v2/manual-review-tasks/{task_id}/resolve",
        json={
            "version": 2,
            "final_score": 10,
            "reason": "人工核对原文后完成评分",
            "evidence": [
                {
                    "evidence_unit_id": evidence_id,
                    "quote": evidence_quote,
                }
            ],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolution_type"] == "human_score"

    item_response = client.get(f"/api/v2/scoring-runs/{run['id']}")
    assert item_response.status_code == 200
    projected = next(
        value for value in item_response.json()["items"] if value["id"] == item_id
    )
    assert projected["final_score"] == 10


def test_exhausted_provider_rule_automatically_opens_blocking_review_task(
    client, monkeypatch
):
    class _FailingScorer(MockLLMScorer):
        def score_core_envelope(self, *, envelope):
            request = httpx.Request(
                "POST", "https://api.groq.test/openai/v1/chat/completions"
            )
            response = httpx.Response(
                503,
                request=request,
                json={"error": {"message": "provider unavailable"}},
            )
            raw = httpx.HTTPStatusError(
                "unsafe response", request=request, response=response
            )
            raise ProviderCallError("groq", project_provider_error(raw)) from raw

    monkeypatch.setattr(lifecycle, "get_llm_scorer", lambda: _FailingScorer())
    _rubric_id, version_id = _publish_fixture(client)
    batch = _create_batch(client, version_id)
    submission, _metadata, _ = _upload(client, batch["id"], variant="complete")

    scored = client.post(
        f"/api/v2/submissions/{submission['id']}/score",
        json={"rescore_generation": 0},
    )
    assert scored.status_code == 200, scored.text
    assert scored.json()["final_total_score"] is None
    assert scored.json()["grade"] is None
    assert scored.json()["need_manual_review"] is True

    tasks = client.get("/api/v2/manual-review-tasks?status=open")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()
    assert all(item["blocking_final_total"] for item in tasks.json())
    assert {
        item["trigger_code"] for item in tasks.json()
    } == {"PROVIDER_UNAVAILABLE"}
