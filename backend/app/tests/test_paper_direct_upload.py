"""Supabase 私有桶直传与可恢复单文件解析的测试先行契约。"""

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch, Paper, Rubric
from backend.app.db.models import utcnow
from backend.app.services.storage import local as storage_module


MIB = 1024 * 1024
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeSupabaseBucket:
    def __init__(self):
        self.objects = {}
        self.signed_paths = []

    def create_signed_upload_url(self, path, options=None):
        self.signed_paths.append((path, options))
        return {
            "signed_url": (
                "https://project-ref.supabase.co/storage/v1/object/upload/sign/"
                f"paper-grading-private/{path}?token=short-lived-token"
            ),
            "token": "short-lived-token",
            "path": path,
        }

    def info(self, path):
        if path not in self.objects:
            error = KeyError(path)
            error.status = "404"
            raise error
        return {"name": Path(path).name, "metadata": {"size": self.objects[path]}}


def _make_batch(client):
    with client.session_factory() as db:
        rubric = Rubric(name="直传测试模板", version="v1", total_score=100)
        db.add(rubric)
        db.flush()
        batch = GradingBatch(name="直传测试批次", rubric_id=rubric.id)
        db.add(batch)
        db.commit()
        return batch.id


def _enable_supabase(monkeypatch):
    bucket = FakeSupabaseBucket()
    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "supabase")
    monkeypatch.setattr(settings, "DIRECT_UPLOAD_MAX_SIZE_MB", 50)
    monkeypatch.setattr(settings, "DIRECT_UPLOAD_TUS_THRESHOLD_MB", 6)
    monkeypatch.setattr(settings, "PAPER_PARSE_LEASE_SECONDS", 600)
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SECRET_KEY", "must-never-leak")
    monkeypatch.setattr(settings, "SUPABASE_STORAGE_BUCKET", "paper-grading-private")
    monkeypatch.setattr(storage_module, "_storage_bucket", lambda bucket_name=None: bucket)
    return bucket


def _create_intent(client, batch_id, *, size=1024, file_name="论文.docx"):
    content_type = "application/pdf" if file_name.lower().endswith(".pdf") else DOCX_TYPE
    return client.post(
        "/api/papers/direct-upload-intents",
        json={
            "batch_id": batch_id,
            "file_name": file_name,
            "content_type": content_type,
            "byte_size": size,
        },
    )


def test_direct_upload_intent_uses_standard_then_tus_at_six_mib(client, monkeypatch):
    bucket = _enable_supabase(monkeypatch)
    batch_id = _make_batch(client)

    standard = _create_intent(client, batch_id, size=6 * MIB)
    assert standard.status_code == 201, standard.text
    standard_body = standard.json()
    assert standard_body["mode"] == "standard"
    assert standard_body["threshold_bytes"] == 6 * MIB
    assert standard_body["paper"]["status"] == "uploading"
    assert standard_body["signed_url"].startswith("https://project-ref.supabase.co/")
    assert standard_body["tus_endpoint"] == (
        "https://project-ref.storage.supabase.co/storage/v1/upload/resumable"
    )

    resumable = _create_intent(client, batch_id, size=6 * MIB + 1, file_name="大论文.pdf")
    assert resumable.status_code == 201, resumable.text
    assert resumable.json()["mode"] == "tus"

    serialized = standard.text + resumable.text
    assert "must-never-leak" not in serialized
    assert all(path.startswith("uploads/") for path, _ in bucket.signed_paths)
    assert all(".." not in path for path, _ in bucket.signed_paths)


def test_direct_upload_intent_rejects_bad_type_size_and_missing_storage(client, monkeypatch):
    batch_id = _make_batch(client)

    unavailable = _create_intent(client, batch_id)
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "DIRECT_STORAGE_NOT_CONFIGURED"

    _enable_supabase(monkeypatch)
    bad_type = _create_intent(client, batch_id, file_name="恶意.exe")
    assert bad_type.status_code == 400
    assert bad_type.json()["detail"]["code"] == "UNSUPPORTED_PAPER_TYPE"

    too_large = _create_intent(client, batch_id, size=settings.DIRECT_UPLOAD_MAX_SIZE_MB * MIB + 1)
    assert too_large.status_code == 413
    assert too_large.json()["detail"]["code"] == "PAPER_FILE_TOO_LARGE"


def test_complete_upload_verifies_private_object_and_is_idempotent(client, monkeypatch):
    bucket = _enable_supabase(monkeypatch)
    batch_id = _make_batch(client)
    intent = _create_intent(client, batch_id, size=3210).json()
    paper_id = intent["paper"]["id"]

    missing = client.post(f"/api/papers/{paper_id}/complete-upload", json={"byte_size": 3210})
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "ARCHIVED_OBJECT_NOT_FOUND"

    bucket.objects[intent["object_path"]] = 3209
    mismatch = client.post(f"/api/papers/{paper_id}/complete-upload", json={"byte_size": 3210})
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "ARCHIVED_OBJECT_SIZE_MISMATCH"

    bucket.objects[intent["object_path"]] = 3210
    completed = client.post(f"/api/papers/{paper_id}/complete-upload", json={"byte_size": 3210})
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "uploaded"

    repeated = client.post(f"/api/papers/{paper_id}/complete-upload", json={"byte_size": 3210})
    assert repeated.status_code == 200
    assert repeated.json()["id"] == paper_id
    assert repeated.json()["status"] == "uploaded"


def test_parse_claim_is_committed_before_work_and_failed_paper_can_retry(client, monkeypatch):
    bucket = _enable_supabase(monkeypatch)
    batch_id = _make_batch(client)
    intent = _create_intent(client, batch_id, size=2000).json()
    paper_id = intent["paper"]["id"]
    bucket.objects[intent["object_path"]] = 2000
    assert client.post(f"/api/papers/{paper_id}/complete-upload", json={"byte_size": 2000}).status_code == 200

    observed_claims = []

    def fail_after_claim(db, paper):
        with client.session_factory() as observer:
            observed_claims.append(observer.get(Paper, paper.id).status)
        paper.status = "failed"
        paper.error_message = "测试解析失败"
        db.flush()
        return paper

    monkeypatch.setattr("backend.app.api.routes.papers.parse_and_store", fail_after_claim)
    failed = client.post(f"/api/papers/{paper_id}/parse")
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert observed_claims == ["parsing"]

    def succeed_after_claim(db, paper):
        with client.session_factory() as observer:
            observed_claims.append(observer.get(Paper, paper.id).status)
        paper.status = "parsed"
        paper.error_message = None
        db.flush()
        return paper

    monkeypatch.setattr("backend.app.api.routes.papers.parse_and_store", succeed_after_claim)
    recovered = client.post(f"/api/papers/{paper_id}/parse")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "parsed"
    assert observed_claims == ["parsing", "parsing"]

    idempotent = client.post(f"/api/papers/{paper_id}/parse")
    assert idempotent.status_code == 200
    assert idempotent.json()["status"] == "parsed"
    assert observed_claims == ["parsing", "parsing"]


def test_parse_rejects_active_claim_but_recovers_stale_claim(client, monkeypatch):
    _enable_supabase(monkeypatch)
    batch_id = _make_batch(client)
    with client.session_factory() as db:
        batch = db.get(GradingBatch, batch_id)
        paper = Paper(
            batch_id=batch_id,
            organization_id=batch.organization_id,
            file_name="论文.docx",
            file_path="supabase://paper-grading-private/uploads/stale.docx",
            status="parsing",
        )
        db.add(paper)
        db.commit()
        paper_id = paper.id

    active = client.post(f"/api/papers/{paper_id}/parse")
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "PAPER_PARSE_IN_PROGRESS"

    with client.session_factory() as db:
        paper = db.get(Paper, paper_id)
        paper.updated_at = utcnow() - timedelta(minutes=20)
        db.commit()

    claims = []

    def recover(db, paper):
        claims.append(paper.status)
        paper.status = "parsed"
        db.flush()
        return paper

    monkeypatch.setattr("backend.app.api.routes.papers.parse_and_store", recover)
    recovered = client.post(f"/api/papers/{paper_id}/parse")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "parsed"
    assert claims == ["parsing"]


def test_web_uses_file_level_direct_upload_tus_fallback_and_visible_recovery():
    root = Path(__file__).resolve().parents[3]
    html = (root / "frontend/web/index.html").read_text(encoding="utf-8")
    script = (root / "frontend/web/assets/app.js").read_text(encoding="utf-8")

    assert 'id="paper-upload-queue"' in html
    assert "/papers/direct-upload-intents" in script
    assert "complete-upload" in script
    assert "uploadViaSignedUrl" in script
    assert 'request.open("PUT", intent.signed_url' in script
    assert 'body.append("cacheControl", "3600")' in script
    assert "uploadViaTus" in script
    assert "Tus-Resumable" in script
    assert "x-signature" in script
    assert "bucketName: intent.bucket_name" in script
    assert "objectName: intent.object_path" in script
    assert "6 * 1024 * 1024" in script
    assert "network" in script and "TUS" in script
    assert "重新解析" in script
    assert 'api("/papers/bulk-upload"' not in script
