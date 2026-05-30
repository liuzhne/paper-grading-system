import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.config import settings
from backend.app.db.models import Base
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCriterion
from backend.app.eval.labeled_dataset import build_labeled_eval
from backend.app.eval.labeled_dataset import load_scores_table
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.tests.conftest import make_sample_docx


def test_load_scores_table_xlsx(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["文件名", "总分", "C01", "C02"])
    sheet.append(["a.docx", 88, 18, 17])
    sheet.append(["b.docx", 72, 12, 14])
    path = tmp_path / "s.xlsx"
    workbook.save(path)

    rows = load_scores_table(str(path))
    assert rows[0] == {"filename": "a.docx", "total": 88.0, "items": {"C01": 18.0, "C02": 17.0}}
    assert len(rows) == 2


def test_load_scores_table_csv(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("filename,total,C01\nx.docx,80,40\n", encoding="utf-8")
    assert load_scores_table(str(path)) == [{"filename": "x.docx", "total": 80.0, "items": {"C01": 40.0}}]


def test_load_scores_table_requires_filename_and_total(tmp_path):
    workbook = Workbook()
    workbook.active.append(["无关列", "另一列"])
    workbook.active.append([1, 2])
    path = tmp_path / "bad.xlsx"
    workbook.save(path)
    with pytest.raises(ValueError):
        load_scores_table(str(path))


def test_build_labeled_eval_pipeline_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path / "storage")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        rubric = Rubric(name="毕设标准", version="v1.0", total_score=20)
        rubric.criteria.append(RubricCriterion(code="C01", name="研究方法", max_score=10))
        rubric.criteria.append(RubricCriterion(code="C02", name="参考文献", max_score=10))
        db.add(rubric)
        db.commit()

        papers = tmp_path / "papers"
        papers.mkdir()
        for name in ("p0.docx", "p1.docx"):
            (papers / name).write_bytes(make_sample_docx().getvalue())

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["文件名", "总分", "C01", "C02"])
        sheet.append(["p0.docx", 18, 9, 9])
        sheet.append(["p1.docx", 14, 7, 7])
        scores = tmp_path / "scores.xlsx"
        workbook.save(scores)

        report = build_labeled_eval(db, rubric.id, str(papers), str(scores), scorer=MockLLMScorer())
        # 验证管线连通（分数为合成，QWK 数值无意义；真实验收需真实教师分）。
        assert report["n"] == 2
        assert report["mae"] is not None
        assert report["qwk"] is not None
        assert "C01" in report["per_criterion"]
        assert report["dataset_size"] == 2
        assert report["errors"] == []
    finally:
        db.close()
