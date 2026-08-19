from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from backend.app.db import models
from backend.app.services.scoring.adapters.persistence import CoreRunPersistence
from backend.app.tests.m3_contract_fixtures import scoring_request_payload
from backend.app.tests.test_m3_legacy_adapters_modes import (
    _InMemoryDocumentSnapshotStore,
)
from backend.app.tests.test_m3_legacy_adapters_modes import _outcome_for
from backend.app.tests.test_m3_legacy_adapters_modes import _persistence_db


def _submission_target(
    db,
    request,
    rubric,
    *,
    snapshot_ref="memory://document-snapshot-m6",
):
    plan = request["plan"]
    document = request["document"]
    submission_payload = request["submission"]
    batch = models.EvaluationBatch(
        id="evaluation-batch-m6",
        name="M6 submission persistence",
        rubric_id=rubric.id,
        rubric_version_id=plan["rubric_version_id"],
        business_profile_key=submission_payload["profile_key"],
        business_profile_version=request["runtime_identity"]["profile_version"],
        status="active",
    )
    submission = models.Submission(
        id=submission_payload["submission_id"],
        evaluation_batch_id=batch.id,
        source_artifact_hash=submission_payload["source_artifact_hash"],
        source_artifact_ref=submission_payload["artifact_refs"][0]["ref"],
        file_name="proposal.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        byte_length=1024,
        submission_metadata=deepcopy(submission_payload["metadata"]),
        status="parsed",
    )
    snapshot = models.DocumentSnapshot(
        id="document-snapshot-m6",
        submission_id=submission.id,
        schema_version=document["schema_version"],
        business_profile_key=document["profile_key"],
        business_profile_version=document["profile_version"],
        parser_version=document["parser_version"],
        normalizer_version=document["normalizer_version"],
        content_hash=document["content_hash"],
        snapshot_hash=document["document_snapshot_hash"],
        snapshot_ref=snapshot_ref,
        snapshot_payload=deepcopy(document),
    )
    db.add_all([batch, submission, snapshot])
    db.commit()
    return batch, submission, snapshot


def test_submission_models_and_target_constraints_are_portable():
    assert models.EvaluationBatch.__tablename__ == "evaluation_batches"
    assert models.Submission.__tablename__ == "submissions"
    assert models.DocumentSnapshot.__tablename__ == "document_snapshots"
    constraints = {item.name for item in models.ScoringRun.__table__.constraints}
    assert "ck_scoring_runs_exactly_one_target" in constraints
    assert "ck_scoring_runs_submission_snapshot_target" in constraints
    postgres_sql = str(
        CreateTable(models.ScoringRun.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "ck_scoring_runs_exactly_one_target" in postgres_sql
    assert "submission_id" in postgres_sql
    assert "document_snapshot_id" in postgres_sql


def test_submission_only_core_run_persists_and_replays_complete_identity():
    engine, db, rubric, criteria, _paper = _persistence_db()
    try:
        request = scoring_request_payload()
        store = _InMemoryDocumentSnapshotStore()
        ref = store.put(request["document"])
        _batch, submission, snapshot = _submission_target(
            db,
            request,
            rubric,
            snapshot_ref=ref,
        )

        run = CoreRunPersistence(
            db,
            document_snapshot_store=store,
        ).persist(
            request=deepcopy(request),
            outcome=_outcome_for(request),
            paper_id=None,
            submission_id=submission.id,
            document_snapshot_id=snapshot.id,
            rubric_id=rubric.id,
            criterion_id_by_code={item.code: item.id for item in criteria},
            workflow_profile="template_driven",
            document_snapshot_ref=ref,
        )

        assert run.paper_id is None
        assert run.submission_id == submission.id
        assert run.document_snapshot_id == snapshot.id
        assert run.submission is submission
        assert run.document_snapshot.snapshot_hash == run.document_snapshot_hash
        assert run.document_snapshot.snapshot_payload == request["document"]
        assert run.business_profile_key == submission.evaluation_batch.business_profile_key
        assert run.rubric_version_id == submission.evaluation_batch.rubric_version_id
        assert submission.status == "scored"

        duplicate = CoreRunPersistence(
            db,
            document_snapshot_store=store,
        ).persist(
            request=deepcopy(request),
            outcome=_outcome_for(request),
            paper_id=None,
            submission_id=submission.id,
            document_snapshot_id=snapshot.id,
            rubric_id=rubric.id,
            criterion_id_by_code={item.code: item.id for item in criteria},
            workflow_profile="template_driven",
            document_snapshot_ref=ref,
        )
        assert duplicate.id == run.id
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("target_mode", ("neither", "both", "wrong-snapshot"))
def test_submission_persistence_rejects_ambiguous_or_mismatched_targets(target_mode):
    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        store = _InMemoryDocumentSnapshotStore()
        ref = store.put(request["document"])
        _batch, submission, snapshot = _submission_target(
            db,
            request,
            rubric,
            snapshot_ref=ref,
        )
        kwargs = {
            "paper_id": None,
            "submission_id": submission.id,
            "document_snapshot_id": snapshot.id,
        }
        if target_mode == "neither":
            kwargs.update(paper_id=None, submission_id=None, document_snapshot_id=None)
        elif target_mode == "both":
            kwargs["paper_id"] = paper.id
        else:
            other = models.Submission(
                id="other-submission-m6",
                evaluation_batch_id=submission.evaluation_batch_id,
                source_artifact_hash="f" * 64,
                source_artifact_ref="blob:sha256:" + "f" * 64,
                file_name="other.docx",
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                byte_length=1,
                submission_metadata={},
                status="parsed",
            )
            db.add(other)
            db.commit()
            kwargs["submission_id"] = other.id

        with pytest.raises(ValueError, match="(?i)(exactly one|snapshot|submission)"):
            CoreRunPersistence(db, document_snapshot_store=store).persist(
                request=deepcopy(request),
                outcome=_outcome_for(request),
                rubric_id=rubric.id,
                criterion_id_by_code={item.code: item.id for item in criteria},
                workflow_profile="template_driven",
                document_snapshot_ref=ref,
                **kwargs,
            )
        assert db.scalar(select(models.ScoringRun)) is None
    finally:
        db.close()
        engine.dispose()


def test_document_snapshot_is_immutable_after_insert_and_sqlite_enforces_target_check():
    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        _batch, submission, snapshot = _submission_target(db, request, rubric)
        snapshot.snapshot_payload = {**snapshot.snapshot_payload, "tampered": True}
        with pytest.raises(ValueError, match="immutable"):
            db.commit()
        db.rollback()

        store = _InMemoryDocumentSnapshotStore()
        ref = store.put(request["document"])
        run = CoreRunPersistence(db, document_snapshot_store=store).persist(
            request=deepcopy(request),
            outcome=_outcome_for(request),
            paper_id=paper.id,
            submission_id=None,
            document_snapshot_id=None,
            rubric_id=rubric.id,
            criterion_id_by_code={item.code: item.id for item in criteria},
            workflow_profile="template_driven",
            document_snapshot_ref=ref,
        )
        with pytest.raises(IntegrityError):
            db.connection().exec_driver_sql(
                "UPDATE scoring_runs SET submission_id = ? WHERE id = ?",
                (submission.id, run.id),
            )
        db.rollback()
    finally:
        db.close()
        engine.dispose()
