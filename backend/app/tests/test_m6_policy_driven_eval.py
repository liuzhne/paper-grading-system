from copy import deepcopy

import pytest

from backend.app.eval import metrics
from backend.app.eval.runner import EvalPrediction
from backend.app.eval.runner import EvalSample
from backend.app.eval.runner import evaluate
from backend.app.eval.run_eval import run_evaluation
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.tests.m2_contract_fixtures import (
    technical_policy_snapshot_payload,
)


def _policy(*, basis="percentage", bands=None, digits=1):
    payload = deepcopy(
        technical_policy_snapshot_payload(
            total_score="20",
            rounding_digits=digits,
        )
    )
    payload.pop("policy_hash")
    payload["grade_scale"] = {
        "basis": basis,
        "bands": bands
        or [
            {"label": "A", "minimum": "90"},
            {"label": "B", "minimum": "75"},
            {"label": "C", "minimum": "60"},
            {"label": "D", "minimum": "0"},
        ],
    }
    payload["review"]["total_below"] = "60" if basis == "percentage" else "10"
    return compile_scoring_policy(payload, total_score="20")


def test_percentage_grade_scale_uses_rubric_total_not_fixed_raw_thresholds():
    scale = metrics.EvaluationGradeScale.from_policy(_policy())

    assert scale.labels == ("D", "C", "B", "A")
    assert scale.ordinal(18) == 3
    assert scale.ordinal(15) == 2
    assert scale.ordinal(12) == 1
    assert scale.ordinal(11.9) == 0
    assert scale.total_score == "20"


def test_evaluate_injects_raw_grade_scale_rounding_and_dynamic_qwk_identity():
    policy = _policy(
        basis="raw_score",
        bands=[
            {"label": "Gold", "minimum": "18"},
            {"label": "Silver", "minimum": "14"},
            {"label": "Bronze", "minimum": "10"},
            {"label": "Retry", "minimum": "0"},
        ],
        digits=1,
    )
    truths = [
        EvalSample("a", 17.96),
        EvalSample("b", 13.96),
    ]
    predictions = [
        EvalPrediction("a", 17.94),
        EvalPrediction("b", 13.94),
    ]

    report = evaluate(predictions, truths, scoring_policy=policy)

    assert report["grade_labels"] == ["Retry", "Bronze", "Silver", "Gold"]
    assert len(report["grade_confusion"]) == 4
    assert report["grade_confusion"][3][2] == 1
    assert report["grade_confusion"][2][1] == 1
    assert report["mae"] == pytest.approx(0.1)
    assert report["rmse"] == pytest.approx(0.1)
    assert report["evaluation_policy_identity"] == {
        "policy_hash": policy.policy_hash,
        "grade_scale_sha256": report["grade_scale_sha256"],
        "rounding": {"mode": "half_up", "digits": 1},
        "total_score": "20",
    }


def test_evaluate_rejects_conflicting_grade_scale_and_policy():
    policy = _policy()
    scale = metrics.EvaluationGradeScale.from_policy(policy)

    with pytest.raises(ValueError, match="either grade_scale or scoring_policy"):
        evaluate([], [], scoring_policy=policy, grade_scale=scale)


def test_offline_eval_entrypoint_forwards_the_injected_scoring_policy(
    tmp_path,
    monkeypatch,
):
    dataset = tmp_path / "empty-eval.json"
    dataset.write_text("[]", encoding="utf-8")
    policy = _policy()
    captured = []

    def fake_evaluate(predictions, samples, **kwargs):
        captured.append((predictions, samples, kwargs))
        return {"n": 0}

    monkeypatch.setattr("backend.app.eval.run_eval.evaluate", fake_evaluate)
    monkeypatch.setattr(
        "backend.app.eval.run_eval._write_report",
        lambda _report: tmp_path / "report.json",
    )

    report = run_evaluation(
        None,
        dataset,
        scoring_policy=policy,
    )

    assert report["n"] == 0
    assert captured == [([], [], {"scoring_policy": policy})]
