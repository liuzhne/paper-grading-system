import json
import re
import shutil
from functools import lru_cache
from hashlib import sha256
from mimetypes import guess_type
from pathlib import Path
from typing import BinaryIO
from backend.app.core.config import settings


def ensure_storage_dirs():
    for path in [settings.uploads_dir, settings.parsed_dir, settings.reports_dir, settings.exports_dir]:
        path.mkdir(parents=True, exist_ok=True)


def safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", filename).strip("._")
    return cleaned or "uploaded_paper"


def save_binary(stream: BinaryIO, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as target:
        shutil.copyfileobj(stream, target)


def _supabase_uri(object_path: str) -> str:
    return "supabase://%s/%s" % (
        settings.SUPABASE_STORAGE_BUCKET,
        object_path.lstrip("/"),
    )


def _parse_supabase_uri(ref: str) -> tuple[str, str]:
    raw = str(ref)
    if not raw.startswith("supabase://"):
        raise ValueError("artifact ref is not a Supabase Storage URI")
    bucket_and_path = raw[len("supabase://") :]
    bucket, separator, object_path = bucket_and_path.partition("/")
    if not separator or not bucket or not object_path:
        raise ValueError("invalid Supabase Storage URI")
    return bucket, object_path


@lru_cache(maxsize=1)
def _supabase_client():
    if not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
        raise RuntimeError(
            "Supabase Storage requires SUPABASE_URL and SUPABASE_SECRET_KEY"
        )
    from supabase import create_client

    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY)


def _storage_bucket(bucket: str | None = None):
    return _supabase_client().storage.from_(
        bucket or settings.SUPABASE_STORAGE_BUCKET
    )


def artifact_ref(namespace: str, filename: str):
    """Return the durable reference used by the configured artifact store."""

    relative = "%s/%s" % (namespace.strip("/"), filename.lstrip("/"))
    if settings.STORAGE_PROVIDER == "supabase":
        return _supabase_uri(relative)
    return str(settings.STORAGE_ROOT / relative)


def store_binary(
    stream: BinaryIO,
    namespace: str,
    filename: str,
    *,
    content_type: str | None = None,
):
    """Persist an artifact and return a local path or private Supabase URI."""

    ref = artifact_ref(namespace, filename)
    if settings.STORAGE_PROVIDER == "local":
        save_binary(stream, Path(ref))
        return ref

    payload = stream.read()
    if not isinstance(payload, bytes):
        payload = bytes(payload)
    bucket, object_path = _parse_supabase_uri(ref)
    options = {
        "content-type": content_type or guess_type(filename)[0] or "application/octet-stream",
        "upsert": "true",
    }
    _storage_bucket(bucket).upload(object_path, payload, options)
    return ref


def store_json(namespace: str, filename: str, payload: dict):
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    from io import BytesIO

    return store_binary(
        BytesIO(encoded),
        namespace,
        filename,
        content_type="application/json; charset=utf-8",
    )


def read_binary(ref) -> bytes:
    raw = str(ref)
    if not raw.startswith("supabase://"):
        return Path(raw).read_bytes()
    bucket, object_path = _parse_supabase_uri(raw)
    return bytes(_storage_bucket(bucket).download(object_path))


def artifact_not_found(exc: Exception) -> bool:
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    status = str(getattr(exc, "status", ""))
    code = str(getattr(exc, "code", "")).casefold()
    return status == "404" or code in {"404", "not_found", "notfound"}


def materialize(ref) -> Path:
    """Return a local file, downloading a private object into the runtime cache."""

    raw = str(ref)
    if not raw.startswith("supabase://"):
        return Path(raw)
    _, object_path = _parse_supabase_uri(raw)
    suffix = Path(object_path).suffix
    digest = sha256(raw.encode("utf-8")).hexdigest()
    cache_dir = settings.STORAGE_ROOT / "object_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / (digest + suffix)
    if not destination.is_file():
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(read_binary(raw))
        temporary.replace(destination)
    return destination


def write_json(path: Path, payload: dict):
    if str(path).startswith("supabase://"):
        bucket, object_path = _parse_supabase_uri(str(path))
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        _storage_bucket(bucket).upload(
            object_path,
            data,
            {"content-type": "application/json; charset=utf-8", "upsert": "true"},
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(read_binary(path).decode("utf-8"))
