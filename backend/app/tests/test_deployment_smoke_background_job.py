"""部署冒烟里的后台评分任务检查：没有 worker 时必须失败，而不是停在排队中。"""

import json

import httpx
import pytest

from backend.app.scripts.smoke_deployment import await_background_job


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _client(statuses, *, item_status="succeeded", error_code="PROVIDER_ERROR", requests=None):
    """Serve job creation, then one status per poll; the last one repeats."""

    remaining = list(statuses)

    def job(status):
        settled = status in ("completed", "completed_with_errors", "failed", "canceled")
        return {
            "id": "job-1",
            "status": status,
            "items": [
                {
                    "paper_id": "paper-1",
                    "status": item_status if settled else "pending",
                    "attempt_count": 1 if settled else 0,
                    "scoring_run_id": "run-1" if item_status == "succeeded" else None,
                    "error_code": None if item_status == "succeeded" else error_code,
                    "error_message": None if item_status == "succeeded" else "模型调用失败",
                }
            ],
        }

    def handler(request):
        if requests is not None:
            requests.append(request)
        status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(200, json=job(status))

    return httpx.Client(base_url="http://smoke", transport=httpx.MockTransport(handler))


def test_waits_for_worker_to_finish_the_uploaded_paper():
    clock, requests = _Clock(), []
    with _client(["queued", "running", "completed"], requests=requests) as client:
        job, item = await_background_job(
            client, {}, batch_id="batch-1", paper_id="paper-1",
            timeout_seconds=30, sleep=clock.sleep, clock=clock,
        )
    assert job["status"] == "completed"
    assert item["scoring_run_id"] == "run-1"
    create = requests[0]
    assert create.method == "POST" and create.url.path == "/api/batches/batch-1/score-jobs"
    assert json.loads(create.content) == {"rescore": False, "max_workers": 1}
    assert [r.url.path for r in requests[1:]] == ["/api/batch-scoring-jobs/job-1"] * 2


def test_job_left_queued_points_at_the_missing_worker():
    clock = _Clock()
    with _client(["queued"]) as client:
        with pytest.raises(RuntimeError, match="is the batch worker running"):
            await_background_job(
                client, {}, batch_id="batch-1", paper_id="paper-1",
                timeout_seconds=10, poll_seconds=2, sleep=clock.sleep, clock=clock,
            )
    assert clock.now >= 10


@pytest.mark.parametrize(
    "final_status, item_status",
    [("completed_with_errors", "failed"), ("failed", "failed"), ("canceled", "canceled")],
)
def test_terminal_without_a_scored_paper_is_a_failure(final_status, item_status):
    clock = _Clock()
    with _client(["running", final_status], item_status=item_status) as client:
        with pytest.raises(RuntimeError, match=final_status):
            await_background_job(
                client, {}, batch_id="batch-1", paper_id="paper-1",
                timeout_seconds=30, sleep=clock.sleep, clock=clock,
            )


def test_fail_closed_accepts_the_missing_model_refusal():
    clock = _Clock()
    with _client(["running", "completed_with_errors"], item_status="failed",
                 error_code="PLATFORM_MODEL_MISSING") as client:
        job, item = await_background_job(
            client, {}, batch_id="batch-1", paper_id="paper-1", timeout_seconds=30,
            scoring="fail-closed", sleep=clock.sleep, clock=clock,
        )
    assert item["error_code"] == "PLATFORM_MODEL_MISSING"
    assert item["attempt_count"] == 1


@pytest.mark.parametrize(
    "item_status, error_code",
    [
        # 没配模型却评出了分：D-028 被绕过，悄悄用了 Mock。
        ("succeeded", None),
        # 失败了，但不是因为缺模型：说明别处坏了，不能当成预期失败放过。
        ("failed", "scoring_failure"),
    ],
)
def test_fail_closed_rejects_scores_and_unrelated_failures(item_status, error_code):
    clock = _Clock()
    final = "completed" if item_status == "succeeded" else "completed_with_errors"
    with _client(["running", final], item_status=item_status, error_code=error_code) as client:
        with pytest.raises(RuntimeError, match="must fail closed"):
            await_background_job(
                client, {}, batch_id="batch-1", paper_id="paper-1", timeout_seconds=30,
                scoring="fail-closed", sleep=clock.sleep, clock=clock,
            )
