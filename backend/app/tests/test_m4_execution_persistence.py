"""M4 rule-decision/effect audit persistence contract.

The executor capability gate is imported from ``test_m4_rule_executor`` so
both suites have exactly one strict-XFAIL boundary.  Once execute_rule_plan
exists, any scoring-outcome@2 or persistence defect is a normal failure.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.services.scoring.adapters.persistence import CoreRunPersistence
from backend.app.services.scoring.core import engine as core_engine
from backend.app.tests.test_m4_rule_executor import (
    _CheckerRegistry,
    _SemanticRuntime,
    _TechnicalProposalProfile,
    _criterion,
    _deduct_rule,
    _plain,
    _quote_occurrence,
    _request_for,
    _require_executor,
    _semantic_response,
    requires_rule_executor,
)


class _DocumentSnapshotStore:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def put(self, snapshot: dict) -> str:
        value = deepcopy(snapshot)
        ref = "memory://document-snapshots/sha256/" + value[
            "document_snapshot_hash"
        ]
        self.objects[ref] = value
        return ref

    def resolve(self, *, ref: str):
        if ref not in self.objects:
            raise KeyError(ref)
        return deepcopy(self.objects[ref])


def _database_graph(request: dict):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    plan = request["plan"]
    user = models.User(
        id="user-m4-rule-audit",
        username="m4-rule-audit",
        display_name="M4 rule audit",
        role="reviewer",
    )
    rubric = models.Rubric(
        id="rubric-m4-rule-audit",
        name="M4 rule audit rubric",
        version="1",
        total_score=100,
        status="published",
        created_by=user.id,
        published_at=now,
    )
    criterion = models.RubricCriterion(
        id="criterion-m4-quality",
        rubric_id=rubric.id,
        code="QUALITY",
        name="Quality",
        max_score=100,
        criterion_type="llm_judgment",
        scoring_mode="deductive",
    )
    compilation = models.RubricCompilation(
        id="compilation-m4-rule-audit",
        rubric_id=rubric.id,
        status="validated",
        parser_version="m4-test-parser@1",
        compiler_version="m4-test-compiler@1",
        prompt_version="m4-test-prompt@1",
        validation_result={"valid": True},
        blockers=[],
        warnings=[],
        created_by=user.id,
        reviewed_by=user.id,
        reviewed_at=now,
        published_at=now,
        final_version_hash=plan["rubric_version_hash"],
    )
    version = models.RubricVersion(
        id=plan["rubric_version_id"],
        rubric_id=rubric.id,
        compilation_id=compilation.id,
        version="4.0.0-test",
        workflow_profile="template_driven",
        global_policy=deepcopy(plan["policy_snapshot"]),
        version_hash=plan["rubric_version_hash"],
        business_profile_key=plan["business_profile_key"],
        hash_scheme=plan["rubric_hash_scheme"],
        created_by=user.id,
    )
    batch = models.GradingBatch(
        id="batch-m4-rule-audit",
        name="M4 rule audit batch",
        rubric_id=rubric.id,
        rubric_version_id=version.id,
        created_by=user.id,
    )
    paper = models.Paper(
        id="paper-m4-rule-audit",
        batch_id=batch.id,
        file_name="m4-audit.docx",
        file_path="memory://m4-audit.docx",
        status="parsed",
    )
    db.add_all([user, rubric, criterion, compilation, version, batch, paper])
    db.commit()
    return engine, db, rubric, criterion, paper


@requires_rule_executor
def test_core_persistence_round_trips_m4_rule_audit_and_retry_is_idempotent():
    _require_executor()
    rule = _deduct_rule(repeat_policy="per_occurrence", max_points="2")
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"],
        occurrences=[
            _quote_occurrence(request, 0),
            _quote_occurrence(request, 1),
        ],
    )
    outcome = core_engine.score_submission(
        request=deepcopy(request),
        checker_registry=_CheckerRegistry(),
        llm_runtime=_SemanticRuntime({rule["rule_code"]: response}),
        profile=_TechnicalProposalProfile(),
    )
    outcome_mapping = _plain(outcome)
    assert outcome_mapping["schema_version"] == "scoring-outcome@2"

    engine, db, rubric, criterion, paper = _database_graph(request)
    try:
        store = _DocumentSnapshotStore()
        document_ref = store.put(request["document"])
        persistence = CoreRunPersistence(db, document_snapshot_store=store)
        kwargs = {
            "request": deepcopy(request),
            "outcome": outcome,
            "paper_id": paper.id,
            "rubric_id": rubric.id,
            "criterion_id_by_code": {criterion.code: criterion.id},
            "workflow_profile": "template_driven",
            "document_snapshot_ref": document_ref,
        }

        first = persistence.persist(**kwargs)
        duplicate = persistence.persist(**kwargs)

        assert duplicate.id == first.id
        assert first.business_profile_version == request["runtime_identity"][
            "profile_version"
        ]
        assert first.prompt_version == request["runtime_identity"]["prompt_version"]
        assert first.runtime_identity == request["runtime_identity"]
        assert db.scalar(select(func.count()).select_from(models.ScoringRun)) == 1
        assert db.scalar(select(func.count()).select_from(models.ScoreItem)) == 1
        item = db.scalar(
            select(models.ScoreItem).where(
                models.ScoreItem.scoring_run_id == first.id
            )
        )
        assert item is not None
        assert item.rule_results_schema_version == "rule-results@2"
        assert len(item.rule_results) == 1
        stored = item.rule_results[0]
        decision = outcome_mapping["rule_decisions"][0]
        assert stored["schema_version"] == "rule-result@2"
        for field in (
            "version_hash",
            "rule_code",
            "criterion_code",
            "direction",
            "effect_type",
            "status",
            "selected_level_code",
            "evidence_refs",
            "occurrences",
            "calculated_effect",
        ):
            assert stored[field] == decision[field]

        occurrence_ids = [
            occurrence["occurrence_id"] for occurrence in stored["occurrences"]
        ]
        assert len(occurrence_ids) == len(set(occurrence_ids)) == 2
        stored_contributions = stored["score_contributions"]
        assert len(stored_contributions) == 2
        assert {
            contribution["occurrence_id"]
            for contribution in stored_contributions
        } == set(occurrence_ids)
        assert item.aggregation_schema_version == "criterion-aggregation@2"
        aggregation_contributions = item.aggregation["contributions"]
        assert [
            contribution["occurrence_id"]
            for contribution in aggregation_contributions
            if contribution["kind"] == "deduction"
        ] == [
            contribution["occurrence_id"]
            for contribution in outcome_mapping["score_contributions"]
            if contribution["kind"] == "deduction"
        ]
        # Reading the same authoritative JSON after a retry proves that the
        # adapter returned the winner rather than appending nested audit rows.
        db.expire_all()
        retried_item = db.scalar(
            select(models.ScoreItem).where(
                models.ScoreItem.scoring_run_id == first.id
            )
        )
        assert retried_item.rule_results == item.rule_results
        assert retried_item.aggregation == item.aggregation
    finally:
        db.close()
        engine.dispose()
