"""评分异常必须收敛批次状态；不访问真实模型。"""
import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.db import models
from backend.app.services.scoring import engine
from backend.app.tests.test_batch_state_machine import _batch


@pytest.mark.parametrize("failure", ["setup", "scoring", "transaction"])
def test_unexpected_scoring_failure_leaves_no_running_batch(client, monkeypatch, failure):
    with client.session_factory() as db:
        batch = _batch(db)
        paper = models.Paper(batch_id=batch.id, file_name="synthetic.docx", file_path="synthetic.docx",
                             status="parsed", parsed_text_path="synthetic.json")
        db.add(paper)
        db.commit()
        batch_id = batch.id
        paper_id = paper.id

        def broken(*args, **kwargs):
            if failure == "transaction":
                db.add(models.Rubric(name=None, version="v1", total_score=100))
                db.flush()  # 真正令 Session 进入 rollback-required 状态。
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(engine, "_scorer_for_batch", broken if failure == "setup" else lambda *a: object())
        monkeypatch.setattr(engine, "_close_scorer", lambda *a: None)
        monkeypatch.setattr(engine, "score_paper", broken)
        with pytest.raises((RuntimeError, IntegrityError)):
            engine.score_batch(db, batch_id)
    with client.session_factory() as db:
        assert db.get(models.GradingBatch, batch_id).status == "scored_with_errors"
        assert db.get(models.Paper, paper_id).status == "parsed"


def test_rejected_second_start_does_not_finish_someone_elses_scoring(client):
    with client.session_factory() as db:
        batch = _batch(db, status="scoring")
        db.commit()
        from backend.app.services.batches.state import BatchStateError
        with pytest.raises(BatchStateError):
            engine.score_batch(db, batch.id)
        db.refresh(batch)
        assert batch.status == "scoring"


def test_platform_scoring_persists_core_result_without_byok_foreign_key(client, monkeypatch):
    from sqlalchemy import select
    from backend.app.services.llm.mock import MockLLMScorer
    from backend.app.tests.test_mode_aware_scoring import _score_single_criterion
    scorer = MockLLMScorer()
    scorer._ai_connection_snapshot = {
        "ai_connection_id": "platform", "key_version": 1,
        "provider_type": "openai_compatible", "model_name": "synthetic-platform-model",
    }
    monkeypatch.setattr(engine, "_scorer_for_batch", lambda *a: scorer)
    _score_single_criterion(client, {
        "code": "D1", "name": "研究方法", "max_score": 20,
        "scoring_mode": "deductive", "display_order": 1,
        "deduction_rules_structured": [{"match": "研究方法论述不足", "points": 20, "reason": "研究方法论述不足"}],
    })
    with client.session_factory() as db:
        run = db.scalar(select(models.ScoringRun))
        assert run.ai_connection_id is None
        assert run.ai_connection_snapshot["ai_connection_id"] == "platform"
        assert db.scalar(select(models.AIUsageLedger)) is None
