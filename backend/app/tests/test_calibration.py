import json
from types import SimpleNamespace

from backend.app.services.llm.openai_compatible_adapter import _input_payload


def _make_rubric(client):
    rubric = {
        "name": "校准测试",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [{"code": "C1", "name": "研究方法", "max_score": 10, "display_order": 1}],
    }
    return client.post("/api/rubrics", json=rubric).json()["id"]


def test_calibration_anchor_create_and_list(client):
    rubric_id = _make_rubric(client)
    create = client.post(
        "/api/calibration/anchors",
        json={
            "rubric_id": rubric_id,
            "criterion_code": "C1",
            "score": 9,
            "max_score": 10,
            "label": "优",
            "excerpt": "脱敏范文：方法严谨、数据来源清楚、可复现。",
            "rationale": "方法完整且可复现，给高分。",
        },
    )
    assert create.status_code == 200, create.text

    listed = client.get("/api/calibration/anchors", params={"rubric_id": rubric_id, "criterion_code": "C1"}).json()
    assert len(listed) == 1
    assert listed[0]["label"] == "优"
    assert listed[0]["source"] == "范文"


def test_calibration_rejects_score_over_max(client):
    rubric_id = _make_rubric(client)
    response = client.post(
        "/api/calibration/anchors",
        json={"rubric_id": rubric_id, "criterion_code": "C1", "score": 11, "max_score": 10, "excerpt": "x"},
    )
    assert response.status_code == 400


def test_calibration_rubric_not_found(client):
    response = client.post(
        "/api/calibration/anchors",
        json={"rubric_id": "missing", "criterion_code": "C1", "score": 5, "max_score": 10, "excerpt": "x"},
    )
    assert response.status_code == 404


def test_input_payload_includes_calibration_anchors():
    criterion = SimpleNamespace(
        id="c1",
        code="C1",
        name="研究方法",
        max_score=10,
        description=None,
        evidence_hints=[],
        deduction_rules=[],
        scoring_mode="llm_direct",
        rubric_levels=[],
    )
    paper = SimpleNamespace(id="p1", title="标题")
    anchors = [{"label": "优", "score": 9, "max_score": 10, "excerpt": "范文", "rationale": "好"}]

    payload = json.loads(_input_payload(paper, criterion, [], [], anchors))
    assert payload["calibration_anchors"][0]["label"] == "优"
