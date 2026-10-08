"""同步评分在受保护部署缺少模型时返回 503（D-028），不是 HTTP 500。"""

from backend.app.api.routes import scoring as scoring_routes
from backend.app.core.config import settings
from backend.app.services.llm.errors import PlatformModelMissingError
from backend.app.tests.conftest import create_legacy_unversioned_rubric_fixture
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.test_m0_characterization import M0_RUBRIC


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _uploaded_paper(client):
    rubric_id = create_legacy_unversioned_rubric_fixture(client, M0_RUBRIC)
    batch = client.post("/api/batches", json={"name": "缺少平台模型", "rubric_id": rubric_id})
    assert batch.status_code == 200, batch.text
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch.json()["id"]},
        files={"file": ("missing-model.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload.status_code == 200, upload.text
    return upload.json()["id"]


def test_sync_scoring_without_a_model_is_refused_with_503(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    paper_id = _uploaded_paper(client)

    def refuse(_db, _paper_id):
        raise PlatformModelMissingError()

    monkeypatch.setattr(scoring_routes, "score_paper", refuse)
    response = client.post(f"/api/papers/{paper_id}/score")

    assert response.status_code == 503
    assert "尚未配置平台模型" in response.json()["detail"]
