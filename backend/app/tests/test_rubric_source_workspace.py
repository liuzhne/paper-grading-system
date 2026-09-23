"""确认后的第一步仍可读取真实文件来源，且沿用评分标准权限。"""
from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.app.api.deps import CurrentPrincipal, current_principal
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.main import app
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.conftest import create_legacy_unversioned_rubric_fixture, publish_rubric_via_api
from backend.app.tests.test_rubric_import_documents_api import _post
from backend.app.tests.test_rubric_import_sessions import _create_session, _rules_with_decimal_scores
from backend.app.tests.test_rubric_parse_coverage_api import _clean_rules


def test_source_workspace_openapi_describes_typed_response(client):
    schema = client.get("/openapi.json").json()
    response = schema["paths"]["/api/rubrics/{rubric_id}/source-workspace"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RubricSourceWorkspaceRead",
    }
    workspace = schema["components"]["schemas"]["RubricSourceWorkspaceRead"]
    assert set(workspace["properties"]) == {
        "rubric_id", "compilation_id", "files", "file_metadata", "criteria",
        "score_adjustments", "previews",
    }


def test_confirmed_source_workspace_keeps_files_and_preview(client):
    draft = _create_session(client, _rules_with_decimal_scores(10.5)).json()
    confirmed = client.post(f"/api/rubrics/import-sessions/{draft['id']}/confirm", json={
        "expected_state_version": 1, "idempotency_key": "source-workspace",
    }).json()
    rubric_id = confirmed["rubric"]["id"]
    result = client.get(f"/api/rubrics/{rubric_id}/source-workspace")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["files"]["rules"] == "rules.xlsx"
    assert body["files"]["template"] is None
    assert body["score_adjustments"][0]["rounded"] == 11
    assert body["criteria"][0]["source_refs"]
    assert any("评分项1" in item["text"] for item in body["previews"]["excel"])
    assert "rules_file_bytes" not in body
    assert "private" in result.headers["cache-control"]
    assert "no-store" in result.headers["cache-control"]
    assert body["file_metadata"]["rules"]["size_bytes"] > 0
    assert body["file_metadata"]["rules"]["uploaded_at"]
    assert body["criteria"][0]["parse_status"] == "rounded"


def test_source_workspace_missing_rubric_is_not_found(client):
    assert client.get("/api/rubrics/missing/source-workspace").status_code == 404


def test_legacy_import_files_keeps_both_sources_without_import_session(client):
    imported = _post(client, rules=fx.simple_rules_xlsx(), template=fx.template_docx_with_comments())
    assert imported.status_code == 200, imported.text
    rubric_id = imported.json()["rubric"]["id"]

    response = client.get(f"/api/rubrics/{rubric_id}/source-workspace")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == {"rules": "rules.xlsx", "template": "template.docx"}
    assert any("方法合理" in item["text"] for item in body["previews"]["excel"])
    assert any("第一章 绪论" in item["text"] for item in body["previews"]["word"])
    assert body["criteria"][0]["source_refs"]
    assert body["score_adjustments"] == []


def test_older_compilation_without_ledger_uses_persisted_source_rows(client):
    imported = _post(client, rules=fx.simple_rules_xlsx()).json()
    rubric_id = imported["rubric"]["id"]
    with client.session_factory() as session:
        compilation = session.scalar(select(models.RubricCompilation).where(models.RubricCompilation.rubric_id == rubric_id))
        raw = deepcopy(compilation.raw_parse_output)
        raw.pop("source_ledger")
        compilation.raw_parse_output = raw
        session.commit()

    response = client.get(f"/api/rubrics/{rubric_id}/source-workspace")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"]["rules"] == "rules.xlsx"
    assert any("方法合理" in item["text"] for item in body["previews"]["excel"])
    assert body["criteria"][0]["source_refs"][0]["sheet_name"] == "评分规则"


def test_published_source_workspace_is_readable_without_changing_graph(client):
    imported = _post(client, rules=_clean_rules()).json()
    rubric_id = imported["rubric"]["id"]
    published, identity = publish_rubric_via_api(client, rubric_id)

    response = client.get(f"/api/rubrics/{rubric_id}/source-workspace")

    assert response.status_code == 200, response.text
    assert response.json()["compilation_id"] == identity["compilation_id"]
    assert response.json()["files"]["rules"] == "rules.xlsx"
    assert any("研究方法" in item["text"] for item in response.json()["previews"]["excel"])
    assert client.get(f"/api/rubrics/{rubric_id}").json() == published


def test_confirmed_session_with_different_file_hash_does_not_override_current_sources(client):
    draft = _create_session(client, _rules_with_decimal_scores(10.5)).json()
    confirmed = client.post(f"/api/rubrics/import-sessions/{draft['id']}/confirm", json={
        "expected_state_version": 1, "idempotency_key": "wrong-session-file",
    }).json()
    rubric_id = confirmed["rubric"]["id"]
    with client.session_factory() as session:
        row = session.get(models.RubricImportSession, draft["id"])
        graph = deepcopy(row.prepared_graph)
        graph["artifacts"][0]["file_hash"] = "0" * 64
        row.prepared_graph = graph
        row.rules_file_name = "unrelated.xlsx"
        row.score_adjustments = [{"code": "OTHER", "rounded": 99}]
        session.commit()

    body = client.get(f"/api/rubrics/{rubric_id}/source-workspace").json()

    assert body["files"]["rules"] == "rules.xlsx"
    assert body["score_adjustments"] == []
    assert any("评分项1" in item["text"] for item in body["previews"]["excel"])


def test_recompilation_keeps_original_file_sources(client):
    imported = _post(client, rules=_clean_rules()).json()
    rubric = imported["rubric"]
    active = client.get(f"/api/rubrics/{rubric['id']}/execution-draft").json()["active_compilation"]
    review = client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()
    recompiled = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": active["id"], "version": "v2",
        "reason": "调整规则后保留原文", "criteria": rubric["criteria"],
        "atomic_rules": [{"id": rule["id"], "content_token": rule["content_token"], "changes": {}}
                         for rule in review["rules"]],
    })
    assert recompiled.status_code == 200, recompiled.text

    response = client.get(f"/api/rubrics/{rubric['id']}/source-workspace")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["compilation_id"] != active["id"]
    assert body["files"]["rules"] == "rules.xlsx"
    assert any("研究方法" in item["text"] for item in body["previews"]["excel"])
    assert body["criteria"][0]["source_refs"]


def test_manual_rubric_does_not_invent_file_sources(client):
    created = client.post("/api/rubrics", json={
        "name": "手工标准", "version": "v1", "total_score": 10,
        "criteria": [{"code": "C01", "name": "分析", "max_score": 10}],
    })
    assert created.status_code == 200, created.text

    response = client.get(f"/api/rubrics/{created.json()['id']}/source-workspace")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == {"rules": None, "template": None}
    assert body["previews"] == {"word": [], "excel": []}
    assert body["criteria"] == [{"code": "C01", "source_refs": [], "parse_status": "unknown"}]


def test_historical_criterion_without_source_is_unknown_not_manual(client):
    rubric_id = create_legacy_unversioned_rubric_fixture(client, {
        "name": "历史标准", "version": "v1", "total_score": 10,
        "criteria": [{"code": "C01", "name": "来源记录缺失", "max_score": 10}],
    })

    response = client.get(f"/api/rubrics/{rubric_id}/source-workspace")

    assert response.status_code == 200, response.text
    assert response.json()["criteria"] == [
        {"code": "C01", "source_refs": [], "parse_status": "unknown"},
    ]


def test_explicit_manual_draft_status_is_retained_without_source_refs(client):
    draft = _create_session(client, _rules_with_decimal_scores(10)).json()
    updated = client.patch(f"/api/rubrics/import-sessions/{draft['id']}", json={
        "expected_state_version": draft["state_version"], "total_score": 15,
        "criteria": [*draft["criteria"], {
            "code": "C02", "name": "人工新增", "max_score": 5, "display_order": 1,
            "source_refs": [], "parse_status": "manual",
        }],
    })
    assert updated.status_code == 200, updated.text
    confirmed = client.post(f"/api/rubrics/import-sessions/{draft['id']}/confirm", json={
        "expected_state_version": updated.json()["state_version"],
        "idempotency_key": "explicit-manual-source",
    })
    assert confirmed.status_code == 200, confirmed.text

    response = client.get(f"/api/rubrics/{confirmed.json()['rubric']['id']}/source-workspace")

    assert response.status_code == 200, response.text
    manual = next(item for item in response.json()["criteria"] if item["code"] == "C02")
    assert manual == {"code": "C02", "source_refs": [], "parse_status": "manual"}


@pytest.mark.parametrize(
    "visibility,owner,viewer_org,platform_role,status",
    [
        ("private", "owner", "source-org", "user", 200),
        ("private", "other", "source-org", "user", 404),
        ("organization", "other", "source-org", "user", 200),
        ("organization", "other", "other-org", "user", 404),
        ("system", "other", "other-org", "user", 200),
        ("private", "other", "other-org", "platform_admin", 200),
    ],
)
def test_source_workspace_inherits_rubric_visibility(client, monkeypatch, visibility, owner, viewer_org, platform_role, status):
    imported = _post(client, rules=fx.simple_rules_xlsx()).json()
    rubric_id = imported["rubric"]["id"]
    with client.session_factory() as session:
        session.add(models.Organization(id="source-org", name="Source organization"))
        session.flush()
        rubric = session.get(models.Rubric, rubric_id)
        rubric.visibility = visibility
        rubric.organization_id = "source-org"
        owner_id = rubric.owner_id
        session.commit()
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "test-only-source-workspace")
    app.dependency_overrides[current_principal] = lambda: CurrentPrincipal(
        user_id=owner_id if owner == "owner" else "another-user",
        organization_id=viewer_org, organization_role="member", platform_role=platform_role,
    )

    response = client.get(f"/api/rubrics/{rubric_id}/source-workspace")

    assert response.status_code == status, response.text
    if status == 404:
        assert response.json() == {"detail": "rubric not found"}


def test_source_workspace_requires_login_when_auth_is_enabled(client, monkeypatch):
    imported = _post(client, rules=fx.simple_rules_xlsx()).json()
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "test-only-source-workspace")

    response = client.get(f"/api/rubrics/{imported['rubric']['id']}/source-workspace")

    assert response.status_code == 401
