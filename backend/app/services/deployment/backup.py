"""Verified PostgreSQL + storage backup/restore packages for PGS-12.

Database credentials are translated to libpq environment variables and never
placed in subprocess argv, manifests, or returned reports.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import uuid

from sqlalchemy.engine import make_url


BACKUP_SCHEMA_VERSION = "paper-grading-backup@1"
DATABASE_DUMP_NAME = "database.dump"
STORAGE_ARCHIVE_NAME = "storage.tar.gz"
MANIFEST_NAME = "manifest.json"


def _default_runner(args, *, env):
    return subprocess.run(
        list(args),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(path):
    path = Path(path)
    return {"size": path.stat().st_size, "sha256": _sha256(path)}


def _postgres_connection(database_url):
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("backup and restore require a PostgreSQL DATABASE_URL")
    if not url.database:
        raise ValueError("PostgreSQL DATABASE_URL must name a database")
    env = dict(os.environ)
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = url.password
    return url, env


def _connection_args(url):
    args = []
    if url.host:
        args.extend(("--host", url.host))
    if url.port:
        args.extend(("--port", str(url.port)))
    if url.username:
        args.extend(("--username", url.username))
    return args


def _archive_storage(storage_root, target):
    storage_root = Path(storage_root).resolve()
    if not storage_root.is_dir():
        raise ValueError("storage_root must be an existing directory")
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(storage_root.rglob("*")):
            if path.is_symlink():
                raise ValueError("storage backup refuses symbolic links")
            if path.is_file():
                archive.add(
                    path,
                    arcname=path.relative_to(storage_root).as_posix(),
                    recursive=False,
                )


def create_backup(
    *,
    database_url,
    storage_root,
    destination_root,
    migration_head,
    revision,
    rto_minutes,
    rpo_minutes,
    command_runner=None,
):
    if not str(migration_head).strip() or not str(revision).strip():
        raise ValueError("migration_head and revision are required")
    if int(rto_minutes) <= 0 or int(rpo_minutes) <= 0:
        raise ValueError("RTO and RPO must be positive minutes")
    runner = command_runner or _default_runner
    storage_root = Path(storage_root).resolve()
    destination_root = Path(destination_root).resolve()
    if destination_root == storage_root or storage_root in destination_root.parents:
        raise ValueError("backup destination must be outside storage_root")
    destination_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stem = "backup-%s-%s-%s" % (
        now.strftime("%Y%m%dT%H%M%SZ"),
        str(revision)[:12],
        uuid.uuid4().hex[:8],
    )
    partial = destination_root / (stem + ".partial")
    final = destination_root / stem
    partial.mkdir()
    try:
        url, env = _postgres_connection(database_url)
        dump = partial / DATABASE_DUMP_NAME
        runner(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file=%s" % dump,
                *_connection_args(url),
                url.database,
            ],
            env=env,
        )
        if not dump.is_file():
            raise RuntimeError("pg_dump completed without creating database.dump")
        archive = partial / STORAGE_ARCHIVE_NAME
        _archive_storage(storage_root, archive)
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": now.isoformat(),
            "migration_head": str(migration_head),
            "revision": str(revision),
            "recovery_objectives": {
                "rto_minutes": int(rto_minutes),
                "rpo_minutes": int(rpo_minutes),
            },
            "assets": {
                DATABASE_DUMP_NAME: _asset(dump),
                STORAGE_ARCHIVE_NAME: _asset(archive),
            },
        }
        (partial / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        partial.rename(final)
        return final
    except Exception:
        if partial.exists():
            shutil.rmtree(partial)
        raise


def verify_backup(package):
    package = Path(package)
    manifest_path = package / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("backup manifest is missing or invalid") from exc
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise ValueError("unsupported backup manifest schema")
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or set(assets) != {
        DATABASE_DUMP_NAME,
        STORAGE_ARCHIVE_NAME,
    }:
        raise ValueError("backup manifest assets are incomplete")
    for name, identity in assets.items():
        path = package / name
        if not path.is_file():
            raise ValueError("backup asset is missing: %s" % name)
        actual = _asset(path)
        if actual != identity:
            raise ValueError("backup checksum or size mismatch: %s" % name)
    return manifest


def _safe_extract(archive_path, destination):
    destination = Path(destination)
    root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if member.issym() or member.islnk():
                raise ValueError("storage archive contains a link")
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError("storage archive contains an unsafe path")
        # Python 3.12 added the safe filter API; retain Python 3.10/3.11
        # compatibility after applying the explicit path/link checks above.
        if "filter" in inspect.signature(archive.extractall).parameters:
            archive.extractall(destination, members=members, filter="data")
        else:
            archive.extractall(destination, members=members)


def restore_backup(
    package,
    *,
    database_url,
    storage_target,
    confirm_database,
    command_runner=None,
):
    manifest = verify_backup(package)
    runner = command_runner or _default_runner
    url, env = _postgres_connection(database_url)
    if confirm_database != url.database:
        raise ValueError("restore confirmation must exactly match target database")
    storage_target = Path(storage_target)
    if storage_target.exists() and any(storage_target.iterdir()):
        raise ValueError("storage restore target must be empty")
    storage_target.parent.mkdir(parents=True, exist_ok=True)
    staging = storage_target.parent / (
        ".%s.restore-partial-%s" % (storage_target.name, uuid.uuid4().hex[:8])
    )
    staging.mkdir()
    try:
        _safe_extract(Path(package) / STORAGE_ARCHIVE_NAME, staging)
        runner(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                *_connection_args(url),
                "--dbname",
                url.database,
                str(Path(package) / DATABASE_DUMP_NAME),
            ],
            env=env,
        )
        if storage_target.exists():
            storage_target.rmdir()
        staging.rename(storage_target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {
        "schema_version": "paper-grading-restore-report@1",
        "database": url.database,
        "storage_target": str(storage_target),
        "backup_schema_version": manifest["schema_version"],
        "migration_head": manifest["migration_head"],
        "revision": manifest["revision"],
    }


__all__ = ["create_backup", "restore_backup", "verify_backup"]
