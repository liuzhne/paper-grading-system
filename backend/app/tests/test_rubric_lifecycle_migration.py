"""P1-03 内容哈希级联迁移验证。"""

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import update

from backend.app.core.config import settings
from backend.app.db.sqlite import enable_sqlite_foreign_keys


ROOT = Path(__file__).resolve().parents[3]
P1_02_REVISION = "0010_atomic_rules"


def _config(url):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_0011_hash_foreign_key_cascades_and_downgrades_without_data_loss(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "p103-hash-cascade.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = _config(url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    metadata = MetaData()
    users = Table("users", metadata, autoload_with=engine)
    rubrics = Table("rubrics", metadata, autoload_with=engine)
    compilations = Table("rubric_compilations", metadata, autoload_with=engine)
    versions = Table("rubric_versions", metadata, autoload_with=engine)
    now = datetime(2026, 7, 17, 9, 0, 0)
    old_hash = "1" * 64
    new_hash = "2" * 64

    with engine.begin() as connection:
        connection.execute(
            users.insert().values(
                id="p103-user",
                username="p103-migration-user",
                display_name="P1-03 迁移用户",
                role="reviewer",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            rubrics.insert().values(
                id="p103-rubric",
                owner_id="p103-user",
                name="P1-03 哈希迁移标准",
                version="1.0.0",
                total_score=100,
                status="review",
                description=None,
                format_spec={},
                created_by="p103-user",
                created_at=now,
                published_at=None,
            )
        )
        connection.execute(
            compilations.insert().values(
                id="p103-compilation",
                rubric_id="p103-rubric",
                status="validated",
                parser_version="p103",
                compiler_version="p103",
                model_provider=None,
                model_name=None,
                sampling_params={},
                prompt_version="p103",
                raw_parse_output={},
                raw_model_output={},
                validation_result={"valid": True},
                blockers=[],
                warnings=[],
                human_changes=[],
                created_by="p103-user",
                reviewed_by=None,
                reviewed_at=None,
                published_at=None,
                final_version_hash=old_hash,
                created_at=now,
            )
        )
        connection.execute(
            versions.insert().values(
                id="p103-version",
                rubric_id="p103-rubric",
                compilation_id="p103-compilation",
                version="1.0.0",
                workflow_profile="template_driven",
                global_policy={},
                version_hash=old_hash,
                created_by="p103-user",
                created_at=now,
            )
        )
        connection.execute(
            update(compilations)
            .where(compilations.c.id == "p103-compilation")
            .values(final_version_hash=new_hash)
        )
        assert connection.scalar(
            select(versions.c.version_hash).where(versions.c.id == "p103-version")
        ) == new_hash

    foreign_key = next(
        item
        for item in inspect(engine).get_foreign_keys("rubric_versions")
        if item["constrained_columns"] == ["compilation_id", "version_hash"]
    )
    assert foreign_key["options"].get("onupdate") == "CASCADE"
    engine.dispose()

    command.downgrade(config, P1_02_REVISION)
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    try:
        versions = Table("rubric_versions", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            assert connection.scalar(
                select(versions.c.version_hash).where(versions.c.id == "p103-version")
            ) == new_hash
        foreign_key = next(
            item
            for item in inspect(engine).get_foreign_keys("rubric_versions")
            if item["constrained_columns"] == ["compilation_id", "version_hash"]
        )
        assert foreign_key["options"].get("onupdate") is None
    finally:
        engine.dispose()
