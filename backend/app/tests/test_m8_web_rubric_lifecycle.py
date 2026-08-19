from pathlib import Path
from io import BytesIO
import json

from openpyxl import Workbook

from backend.app.tests.conftest import make_template_docx


ROOT = Path(__file__).resolve().parents[3]


def _published_rule_workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Atomic Rules"
    sheet.append(
        [
            "原子规则编号",
            "评分项编号",
            "评分项",
            "分值",
            "类型",
            "评分模式",
            "评分说明",
            "证据提示",
            "适用范围",
            "分档",
            "evidence_policy",
            "effect_type",
            "strictness",
        ]
    )
    sheet.append(
        [
            "thesis.method_quality.v1",
            "METHOD",
            "研究方法质量",
            10,
            "semantic",
            "banded",
            "研究方法应完整且可复现。",
            "研究方法；实验设计",
            "研究方法",
            "HIGH:10;LOW:5",
            json.dumps(
                {
                    "mode": "source_quote",
                    "requirement": "required",
                    "minimum_coverage": "1",
                }
            ),
            "score",
            "required",
        ]
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _import_rubric(client, *, name: str):
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": name, "version": "v1.0", "description": "PGS-5 Web E2E"},
        files={
            "template_file": (
                "template.docx",
                make_template_docx().getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            "rules_file": (
                "rules.xlsx",
                _published_rule_workbook_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload["warnings"], list)
    assert payload["template_summary"]["hints"]
    assert payload["rubric"]["status"] == "draft"
    return payload


def _execution_draft(client, rubric_id: str):
    response = client.get(f"/api/rubrics/{rubric_id}/execution-draft")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["rubric_id"] == rubric_id
    assert payload["active_compilation"] is not None
    return payload


def test_static_web_exposes_import_preflight_and_strict_lifecycle_controls():
    html = (ROOT / "frontend/web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend/web/assets/app.js").read_text(encoding="utf-8")

    for element_id in (
        "rubric-import-preview",
        "rubric-lifecycle-select",
        "rubric-lifecycle-summary",
        "rubric-lifecycle-blockers",
        "rubric-lifecycle-rules",
        "rubric-lifecycle-template-links",
    ):
        assert f'id="{element_id}"' in html

    for marker in (
        "rubricImportPreview",
        "/execution-draft",
        "/submit-review",
        "/return-to-draft",
        "/rules/",
        "/approve",
        "/reject",
        "/reopen",
        "/template-links/",
        "compilation_id",
    ):
        assert marker in script

    assert "await api(`/rubrics/${rubricId}/publish`" in script
    assert "body: JSON.stringify({ compilation_id:" in script
    assert "await api(\"/rubrics/import-files\"" in script
    assert "state.rubricImportPreview = imported" in script


def test_web_api_lifecycle_import_review_approve_confirm_and_publish(client):
    imported = _import_rubric(client, name="PGS-5 Web lifecycle")
    rubric_id = imported["rubric"]["id"]
    draft = _execution_draft(client, rubric_id)
    active = draft["active_compilation"]
    assert active["version"]["business_profile_key"] == "thesis"
    assert active["rules"]

    for rule in active["rules"]:
        submitted = client.post(
            f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/submit-review",
            json={"reason": "PGS-5 Web 提交规则审核"},
        )
        assert submitted.status_code == 200, submitted.text
        approved = client.post(
            f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/approve",
            json={"reason": "PGS-5 Web 批准规则"},
        )
        assert approved.status_code == 200, approved.text

    for link in active["template_links"]:
        reviewed = client.post(
            f"/api/rubrics/{rubric_id}/template-links/{link['id']}/review",
            json={"decision": "confirmed", "reason": "PGS-5 Web 确认模板映射"},
        )
        assert reviewed.status_code == 200, reviewed.text

    submitted = client.post(f"/api/rubrics/{rubric_id}/submit-review")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "review"

    missing_identity = client.post(f"/api/rubrics/{rubric_id}/publish")
    assert missing_identity.status_code == 400
    assert missing_identity.json()["detail"] == (
        "compilation_id is required for provenance publication"
    )

    published = client.post(
        f"/api/rubrics/{rubric_id}/publish",
        json={
            "compilation_id": active["id"],
            "reason": "PGS-5 Web 显式发布活动 compilation",
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    frozen = _execution_draft(client, rubric_id)
    assert frozen["rubric_status"] == "published"
    assert frozen["active_compilation"]["id"] == active["id"]
    assert all(rule["status"] == "approved" for rule in frozen["active_compilation"]["rules"])


def test_web_api_publish_is_blocked_before_rule_approval(client):
    imported = _import_rubric(client, name="PGS-5 blocked lifecycle")
    rubric_id = imported["rubric"]["id"]
    active = _execution_draft(client, rubric_id)["active_compilation"]

    submitted = client.post(f"/api/rubrics/{rubric_id}/submit-review")
    assert submitted.status_code == 200, submitted.text
    blocked = client.post(
        f"/api/rubrics/{rubric_id}/publish",
        json={
            "compilation_id": active["id"],
            "reason": "PGS-5 验证非法状态阻断",
        },
    )
    assert blocked.status_code == 400
    assert blocked.json()["detail"]
    assert client.get(f"/api/rubrics/{rubric_id}").json()["status"] == "review"
