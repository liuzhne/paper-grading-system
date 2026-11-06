import hashlib
import json
import os
from pathlib import Path
import subprocess

from backend.app.services.deployment.backup import create_backup
from backend.app.services.deployment.backup import restore_backup
from backend.app.services.deployment.backup import verify_backup


CONTAINER = "pgs-codex-postgres-20260805"
ROOT = Path("/private/tmp/pgs-codex-smoke.0EdFlX")
CONTAINER_DUMP = "/tmp/pgs-codex-database.dump"
CONTAINER_RESTORE = "/tmp/pgs-codex-restore.dump"


def _docker_exec(command, args, env):
    return subprocess.run(
        [
            "docker",
            "exec",
            "--env",
            "PGPASSWORD=%s" % env.get("PGPASSWORD", ""),
            CONTAINER,
            command,
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _container_args(args, *, file_value):
    converted = []
    index = 1
    while index < len(args):
        value = args[index]
        if value.startswith("--file="):
            converted.append("--file=%s" % file_value)
        elif value == "--host":
            converted.extend(("--host", "127.0.0.1"))
            index += 1
        elif value == "--port":
            converted.extend(("--port", "5432"))
            index += 1
        elif index == len(args) - 1 and args[0] == "pg_restore":
            converted.append(file_value)
        else:
            converted.append(value)
        index += 1
    return converted


def docker_postgres_runner(args, *, env):
    args = list(args)
    if args[0] == "pg_dump":
        host_dump = Path(
            next(value.split("=", 1)[1] for value in args if value.startswith("--file="))
        )
        result = _docker_exec(
            "pg_dump",
            _container_args(args, file_value=CONTAINER_DUMP),
            env,
        )
        subprocess.run(
            ["docker", "cp", "%s:%s" % (CONTAINER, CONTAINER_DUMP), str(host_dump)],
            check=True,
            capture_output=True,
            text=True,
        )
        return result
    if args[0] == "pg_restore":
        host_dump = Path(args[-1])
        subprocess.run(
            ["docker", "cp", str(host_dump), "%s:%s" % (CONTAINER, CONTAINER_RESTORE)],
            check=True,
            capture_output=True,
            text=True,
        )
        return _docker_exec(
            "pg_restore",
            _container_args(args, file_value=CONTAINER_RESTORE),
            env,
        )
    raise AssertionError("unexpected PostgreSQL command: %s" % args[0])


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


source_storage = ROOT / "pg-storage"
backup_root = ROOT / "pg-backups"
restored_storage = ROOT / "pg-restored"
database_url = (
    "postgresql+psycopg://paper:codex-only-postgres-password@"
    "127.0.0.1:49355/paper_grading"
)
recovery_url = (
    "postgresql+psycopg://paper:codex-only-postgres-password@"
    "127.0.0.1:49355/recovery_db"
)

package = create_backup(
    database_url=database_url,
    storage_root=source_storage,
    destination_root=backup_root,
    migration_head="0017_batch_scoring_jobs",
    revision="local-codex-postgres-drill",
    rto_minutes=120,
    rpo_minutes=1440,
    command_runner=docker_postgres_runner,
)
manifest = verify_backup(package)
restore_report = restore_backup(
    package,
    database_url=recovery_url,
    storage_target=restored_storage,
    confirm_database="recovery_db",
    command_runner=docker_postgres_runner,
)
storage_match = {
    relative: digest(source_storage / relative) == digest(restored_storage / relative)
    for relative in (
        "uploads/fixture.docx",
        "parsed/snapshot.json",
        "llm_cache.sqlite",
    )
}
print(
    json.dumps(
        {
            "backup_schema_version": manifest["schema_version"],
            "migration_head": manifest["migration_head"],
            "database_dump_size": manifest["assets"]["database.dump"]["size"],
            "storage_archive_size": manifest["assets"]["storage.tar.gz"]["size"],
            "restore_report": restore_report,
            "storage_match": storage_match,
            "all_storage_matches": all(storage_match.values()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
