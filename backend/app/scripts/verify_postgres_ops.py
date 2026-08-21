"""PGS-12 Postgres gate.

Verifies postgresql, alembic_version, the
ix_batch_scoring_jobs_one_active_per_batch predicate and stable
``ORDER BY created_at, id``.  The resulting report always carries
production_default_switch_authorized=false.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.services.deployment.postgres_verifier import (
    exercise_postgres_constraints,
)
from backend.app.services.deployment.postgres_verifier import verify_postgres


def main(argv=None):
    parser = argparse.ArgumentParser(prog="verify_postgres_ops")
    parser.add_argument("--exercise-ci-fixture", action="store_true")
    args = parser.parse_args(argv)
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session:
            report = verify_postgres(session)
        if args.exercise_ci_fixture:
            with factory() as session:
                report["constraint_exercise"] = exercise_postgres_constraints(
                    session
                )
            with factory() as session:
                report["post_exercise"] = verify_postgres(session)
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
