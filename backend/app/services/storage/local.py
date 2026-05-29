import json
import re
import shutil
from pathlib import Path
from typing import BinaryIO

from backend.app.core.config import settings


def ensure_storage_dirs():
    for path in [settings.uploads_dir, settings.parsed_dir, settings.reports_dir, settings.exports_dir]:
        path.mkdir(parents=True, exist_ok=True)


def safe_filename(filename):
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", filename).strip("._")
    return cleaned or "uploaded_paper"


def save_binary(stream: BinaryIO, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as target:
        shutil.copyfileobj(stream, target)


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8"))
