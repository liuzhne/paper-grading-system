from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy import inspect

from backend.app.core.config import settings

ROOT = Path(__file__).resolve().parents[3]


def test_alembic_migrations_apply_to_head(monkeypatch, tmp_path):
    """pytest 平时用 create_all 建表，不走 Alembic；这里独立验证 0001-0008 迁移链能干净升级到 head。"""
    url = "sqlite+pysqlite:///%s" % (tmp_path / "migrations.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)

    # 不传 alembic.ini：使 config_file_name=None，env.py 跳过 fileConfig，
    # 避免 disable_existing_loggers 把 app 日志器禁掉而污染其它用例。
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        # 0001 基础表 + 0004 校准锚点表
        assert {"users", "rubrics", "rubric_criteria", "scoring_runs", "score_items", "calibration_anchors"}.issubset(tables)
        # 0002 原子项语义
        criterion_cols = {col["name"] for col in inspector.get_columns("rubric_criteria")}
        assert {"criterion_type", "scoring_mode", "applies_to", "rubric_levels", "sub_checks"}.issubset(criterion_cols)
        # 0007 维度 + 结构化扣分规则
        assert {"dimension", "deduction_rules_structured"}.issubset(criterion_cols)
        # 0002 结构化扣分 + 0003 篇章一致性 + 0006 格式问题 + token 计量
        score_item_cols = {col["name"] for col in inspector.get_columns("score_items")}
        assert {"deduction_items", "band_selection", "sub_results"}.issubset(score_item_cols)
        run_cols = {col["name"] for col in inspector.get_columns("scoring_runs")}
        assert {"prompt_tokens", "total_tokens", "coherence_findings", "format_findings"}.issubset(run_cols)
        # 0005 模板格式规格
        assert "format_spec" in {col["name"] for col in inspector.get_columns("rubrics")}
        # 0008 owner_id 预留（单租户起步，为多用户铺路）
        for table in ("rubrics", "grading_batches", "papers", "scoring_runs"):
            assert "owner_id" in {col["name"] for col in inspector.get_columns(table)}
    finally:
        engine.dispose()
