"""CLI for verified production backup, verification and isolated restore."""

from __future__ import annotations

import argparse
import json
import os

from backend.app.core.config import settings
from backend.app.services.deployment.backup import create_backup
from backend.app.services.deployment.backup import restore_backup
from backend.app.services.deployment.backup import verify_backup


def _parser():
    parser = argparse.ArgumentParser(prog="ops_backup")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--database-url", default=settings.DATABASE_URL)
    create.add_argument("--storage-root", default=str(settings.STORAGE_ROOT))
    create.add_argument("--destination", required=True)
    create.add_argument("--migration-head", default="0022_legacy_tenant_backfill")
    create.add_argument("--revision", default=os.environ.get("GITHUB_SHA", "local"))
    create.add_argument("--rto-minutes", type=int, default=settings.OPS_RTO_MINUTES)
    create.add_argument("--rpo-minutes", type=int, default=settings.OPS_RPO_MINUTES)
    verify = commands.add_parser("verify")
    verify.add_argument("package")
    restore = commands.add_parser("restore")
    restore.add_argument("package")
    restore.add_argument("--database-url", required=True)
    restore.add_argument("--storage-target", required=True)
    restore.add_argument("--confirm-database", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "create":
        package = create_backup(
            database_url=args.database_url,
            storage_root=args.storage_root,
            destination_root=args.destination,
            migration_head=args.migration_head,
            revision=args.revision,
            rto_minutes=args.rto_minutes,
            rpo_minutes=args.rpo_minutes,
        )
        report = verify_backup(package)
        report["package"] = str(package)
    elif args.command == "verify":
        report = verify_backup(args.package)
    else:
        report = restore_backup(
            args.package,
            database_url=args.database_url,
            storage_target=args.storage_target,
            confirm_database=args.confirm_database,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
