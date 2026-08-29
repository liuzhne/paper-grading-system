import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.pool import NullPool
from storage3.exceptions import StorageApiError

from backend.app.core.config import Settings, settings
from backend.app.db.session import _engine_options
from backend.app.services.storage import local as artifact_storage


ROOT = Path(__file__).resolve().parents[3]


class _FakeBucket:
    def __init__(self, objects):
        self.objects = objects

    def upload(self, path, payload, options):
        self.objects[path] = bytes(payload)
        return {"path": path, "options": options}

    def download(self, path):
        if path not in self.objects:
            raise KeyError(path)
        return self.objects[path]


class _FakeStorage:
    def __init__(self):
        self.objects = {}

    def from_(self, bucket):
        return _FakeBucket(self.objects.setdefault(bucket, {}))


class _FakeSupabase:
    def __init__(self):
        self.storage = _FakeStorage()


def test_supabase_artifacts_round_trip_and_materialize(monkeypatch, tmp_path):
    saved = (
        settings.STORAGE_PROVIDER,
        settings.STORAGE_ROOT,
        settings.SUPABASE_STORAGE_BUCKET,
    )
    fake = _FakeSupabase()
    monkeypatch.setattr(artifact_storage, "_supabase_client", lambda: fake)
    settings.STORAGE_PROVIDER = "supabase"
    settings.STORAGE_ROOT = tmp_path
    settings.SUPABASE_STORAGE_BUCKET = "private-documents"
    try:
        source_ref = artifact_storage.store_binary(
            BytesIO(b"document-bytes"),
            "uploads",
            "paper.pdf",
            content_type="application/pdf",
        )
        parsed_ref = artifact_storage.store_json(
            "parsed", "paper.json", {"title": "Test"}
        )

        assert source_ref == "supabase://private-documents/uploads/paper.pdf"
        assert artifact_storage.read_binary(source_ref) == b"document-bytes"
        assert artifact_storage.read_json(parsed_ref) == {"title": "Test"}
        local_path = artifact_storage.materialize(source_ref)
        assert local_path.suffix == ".pdf"
        assert local_path.read_bytes() == b"document-bytes"
    finally:
        (
            settings.STORAGE_PROVIDER,
            settings.STORAGE_ROOT,
            settings.SUPABASE_STORAGE_BUCKET,
        ) = saved


def test_supabase_missing_object_error_is_normalized():
    assert artifact_storage.artifact_not_found(
        StorageApiError("Object not found", "not_found", 404)
    )


def test_supabase_transaction_pooler_disables_client_pool_and_prepares():
    options = _engine_options(
        "postgresql+psycopg://postgres.example:secret@aws-0.pooler.supabase.com:6543/postgres"
    )
    assert options["poolclass"] is NullPool
    assert options["connect_args"] == {"prepare_threshold": None}


def test_vercel_postgres_url_alias_uses_psycopg(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "postgres://postgres.example:secret@aws-0.pooler.supabase.com:6543/postgres?sslmode=require&supa=base-pooler.x&pgbouncer=true",
    )
    configured = Settings(_env_file=None)
    assert configured.DATABASE_URL.startswith("postgresql+psycopg://")
    assert parse_qs(urlsplit(configured.DATABASE_URL).query) == {
        "sslmode": ["require"]
    }


def test_vercel_entrypoint_and_bundle_contract():
    assert (ROOT / "api" / "index.py").read_text(encoding="utf-8").endswith(
        '__all__ = ["app"]\n'
    )
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["regions"] == ["sin1"]
    function = config["functions"]["api/**/*.py"]
    assert function["maxDuration"] == 60
    assert ".env.*" in function["excludeFiles"]
    assert "frontend/**" in function["excludeFiles"]
    ignore_rules = (ROOT / ".vercelignore").read_text(encoding="utf-8").splitlines()
    assert "/storage" in ignore_rules
    assert "storage" not in ignore_rules
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "npm install --global vercel@58.4.0" in workflow
    assert "vercel deploy --prebuilt --prod" in workflow
    deployment_guide = (ROOT / "docs" / "部署.md").read_text(encoding="utf-8")
    for marker in (
        "GitHub Actions 门禁后部署",
        "git.deploymentEnabled=false",
        "deploy-vercel-production",
        "VERCEL_TOKEN",
        "vercel@58.4.0",
        "vercel/vercel#17386",
    ):
        assert marker in deployment_guide
