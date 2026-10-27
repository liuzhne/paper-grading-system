"""每晚备份：生产 compose 的 backup 服务常驻运行（docs/迁出Supabase到单机Docker方案.md 第 7 节）。

每天 03:00（UTC+8）用与 ``ops_backup create`` 相同的 ``create_backup`` 做一份带校验清单的备份
（``pg_dump`` + 存储目录打包），校验通过后只留最近 7 份。

- 用超级用户连接（``POSTGRES_PASSWORD``，只在 db.env 里）：``pg_dump`` 默认关闭行级安全，
  运行角色 ``pgs_app`` 没有绕过行级安全的权限，会直接报错。
- 数据还在 Supabase（``PGS_DATA_PLANE`` 不是 ``local``）时只记一行日志、不备份。
- 目标卷空闲不足 5 GB 时不备份、记错误日志；不删旧备份腾地方。
- ``--once``：立即做一份并校验，失败时退出码非零（切换工作流核对用）。

    python -m backend.app.scripts.nightly_backup --storage-root /app/storage --destination /backups [--once]

日志只有包名、大小与异常类型，不含连接串、密码或数据内容。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
from pathlib import Path
import shutil
import signal
from threading import Event

from backend.app.core.config import settings
from backend.app.core.log_config import configure_logging
from backend.app.services.deployment.backup import create_backup
from backend.app.services.deployment.backup import current_migration_head
from backend.app.services.deployment.backup import verify_backup


logger = logging.getLogger("ops-backup")

#: 维护者在马来西亚（UTC+8）；用固定偏移，不依赖镜像里的时区数据。
LOCAL_TIMEZONE = timezone(timedelta(hours=8), "UTC+8")
RUN_HOUR = 3
KEEP = 7
MIN_FREE_BYTES = 5 * 1024**3
PACKAGE_PREFIX = "backup-"


def backup_database_url(environ) -> str | None:
    """本机 db 的超级用户连接串；密码只有字母和数字（scripts/production_env.py），不用编码。"""

    password = environ.get("POSTGRES_PASSWORD")
    if not password:
        return None
    return "postgresql://postgres:%s@db:5432/pgs" % password


def next_run(now: datetime, hour: int = RUN_HOUR) -> datetime:
    local = now.astimezone(LOCAL_TIMEZONE)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


def packages(destination: Path) -> list[Path]:
    """已完成的备份包，旧的在前（包名以 UTC 时间开头，按名字排序即按时间）。"""

    if not destination.is_dir():
        return []
    return sorted(
        path
        for path in destination.iterdir()
        if path.is_dir() and path.name.startswith(PACKAGE_PREFIX) and not path.name.endswith(".partial")
    )


def prune(destination: Path, keep: int = KEEP) -> list[str]:
    existing = packages(destination)
    removed = []
    for path in existing[: max(0, len(existing) - keep)]:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def _size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def run_once(
    destination,
    storage_root,
    environ,
    *,
    create=create_backup,
    verify=verify_backup,
    disk_usage=shutil.disk_usage,
    min_free_bytes=MIN_FREE_BYTES,
    keep=KEEP,
):
    """做一份备份并校验；返回包路径，没做（不是本机数据、空间不足）时返回 None。"""

    plane = environ.get("PGS_DATA_PLANE") or "supabase"
    if plane != "local":
        logger.info("nightly_backup_skipped reason=data_plane plane=%s", plane)
        return None
    database_url = backup_database_url(environ)
    if database_url is None:
        logger.error("nightly_backup_skipped reason=missing_postgres_password")
        return None
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    free = disk_usage(destination).free
    if free < min_free_bytes:
        logger.error(
            "nightly_backup_skipped reason=low_disk free_gb=%.1f min_gb=%.1f",
            free / 1024**3,
            min_free_bytes / 1024**3,
        )
        return None
    package = create(
        database_url=database_url,
        storage_root=storage_root,
        destination_root=destination,
        migration_head=current_migration_head(),
        revision="nightly",
        rto_minutes=settings.OPS_RTO_MINUTES,
        rpo_minutes=settings.OPS_RPO_MINUTES,
    )
    verify(package)
    removed = prune(destination, keep)
    logger.info(
        "nightly_backup_created package=%s size_mb=%.1f kept=%s removed=%s",
        Path(package).name,
        _size(Path(package)) / 1024**2,
        len(packages(destination)),
        len(removed),
    )
    return Path(package)


def _attempt(args, environ) -> bool:
    try:
        run_once(args.destination, args.storage_root, environ)
        return True
    except Exception as exc:
        # 异常文本可能带 pg_dump 的连接信息：只记类型。
        logger.error("nightly_backup_failed error_type=%s", type(exc).__name__)
        return False


def main(argv=None, environ=None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--destination", required=True, help="备份目录（生产：卷 backups 挂在 /backups）")
    parser.add_argument("--storage-root", default=str(settings.STORAGE_ROOT), help="要一起打包的存储目录")
    parser.add_argument("--once", action="store_true", help="立即做一份并校验后退出")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    configure_logging(settings.LOG_LEVEL)

    if args.once:
        try:
            package = run_once(args.destination, args.storage_root, environ)
        except Exception as exc:
            logger.error("nightly_backup_failed error_type=%s", type(exc).__name__)
            return 1
        return 0 if package is not None else 1

    stop = Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        target = next_run(datetime.now(timezone.utc))
        logger.info("nightly_backup_scheduled at=%s", target.isoformat())
        if stop.wait(max(0.0, (target - datetime.now(timezone.utc)).total_seconds())):
            break
        _attempt(args, environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
