"""M3 legacy adapters, Core persistence and rollout-mode contracts."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.config import Settings, settings
from backend.app.db import models
from backend.app.db.session import get_db
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.main import app
from backend.app.services.storage.local import ensure_storage_dirs
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import (
    CompiledRubricSnapshot,
    DocumentSnapshot,
    SubmissionSnapshot,
)
from backend.app.services.scoring.core.results import ScoringOutcome
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.m2_contract_fixtures import (
    document_snapshot_projection,
    technical_policy_snapshot_payload,
)
from backend.app.tests.m3_contract_fixtures import (
    DETERMINISTIC_RULE_CODE,
    SEMANTIC_RULE_CODE,
    idempotency_projection,
    plan_hash_projection,
    scoring_request_payload,
)
from backend.app.tests.test_m0_characterization import M0_RUBRIC


PAPER_ADAPTER_MODULE = "backend.app.services.scoring.adapters.legacy_paper"
RUBRIC_ADAPTER_MODULE = "backend.app.services.scoring.adapters.legacy_rubric"
PERSISTENCE_MODULE = "backend.app.services.scoring.adapters.persistence"
COMPARISON_MODULE = "backend.app.services.scoring.adapters.comparison"
CORE_ENGINE_MODULE = "backend.app.services.scoring.core.engine"
CHECKER_REGISTRY_MODULE = "backend.app.services.scoring.core.checker_registry"
EXECUTION_PLAN_MODULE = "backend.app.services.scoring.core.execution_plan"


class M3CapabilityUnavailable(RuntimeError):
    """The only exception a missing M3 capability may turn into XFAIL."""


def _probe_symbol(module_name: str, symbol: str):
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        target_missing = exc.name == module_name or (
            exc.name is not None and module_name.startswith(exc.name + ".")
        )
        if not target_missing:
            raise
        return None
    return vars(module).get(symbol)


_LEGACY_PAPER_ADAPTER = _probe_symbol(PAPER_ADAPTER_MODULE, "LegacyPaperAdapter")
_LEGACY_RUBRIC_ADAPTER = _probe_symbol(RUBRIC_ADAPTER_MODULE, "LegacyRubricAdapter")
_CORE_RUN_PERSISTENCE = _probe_symbol(PERSISTENCE_MODULE, "CoreRunPersistence")
_COMPARISON_SINK = _probe_symbol(COMPARISON_MODULE, "ComparisonArtifactSink")
_GET_COMPARISON_SINK = _probe_symbol(
    COMPARISON_MODULE,
    "get_comparison_artifact_sink",
)
_SCORE_SUBMISSION = _probe_symbol(CORE_ENGINE_MODULE, "score_submission")
_VERSIONED_CHECKER_REGISTRY = _probe_symbol(
    CHECKER_REGISTRY_MODULE,
    "VersionedCheckerRegistry",
)
_RULE_EXECUTION_PLAN_BUILDER = _probe_symbol(
    EXECUTION_PLAN_MODULE,
    "RuleExecutionPlanBuilder",
)
_MODE_SETTING_READY = (
    "SCORING_ENGINE_MODE" in Settings.model_fields
    and hasattr(settings, "SCORING_ENGINE_MODE")
)


def _requires(label: str, condition: bool):
    return pytest.mark.xfail(
        condition=condition,
        reason=f"M3 capability is not implemented: {label}",
        raises=M3CapabilityUnavailable,
        strict=True,
    )


requires_paper_adapter = _requires(
    f"{PAPER_ADAPTER_MODULE}.LegacyPaperAdapter",
    _LEGACY_PAPER_ADAPTER is None,
)
requires_rubric_adapter = _requires(
    f"{RUBRIC_ADAPTER_MODULE}.LegacyRubricAdapter",
    _LEGACY_RUBRIC_ADAPTER is None,
)
requires_persistence = _requires(
    f"{PERSISTENCE_MODULE}.CoreRunPersistence",
    _CORE_RUN_PERSISTENCE is None,
)
requires_modes = _requires(
    "Settings.SCORING_ENGINE_MODE",
    not _MODE_SETTING_READY,
)
requires_compare = _requires(
    "compare rollout with score_submission and both legacy adapters",
    not _MODE_SETTING_READY
    or _SCORE_SUBMISSION is None
    or _LEGACY_PAPER_ADAPTER is None
    or _LEGACY_RUBRIC_ADAPTER is None
    or _COMPARISON_SINK is None
    or _GET_COMPARISON_SINK is None,
)
requires_core_mode = _requires(
    "core rollout route with plan, adapters, score_submission and persistence",
    not _MODE_SETTING_READY
    or _SCORE_SUBMISSION is None
    or _VERSIONED_CHECKER_REGISTRY is None
    or _RULE_EXECUTION_PLAN_BUILDER is None
    or _LEGACY_PAPER_ADAPTER is None
    or _LEGACY_RUBRIC_ADAPTER is None
    or _CORE_RUN_PERSISTENCE is None,
)


def _require(value, label):
    if value is None:
        raise M3CapabilityUnavailable(f"{label} is not implemented")
    return value


def _require_mode_setting():
    if not _MODE_SETTING_READY:
        raise M3CapabilityUnavailable("Settings.SCORING_ENGINE_MODE is not implemented")


def _require_core_mode_capabilities():
    _require_mode_setting()
    for value, label in (
        (_SCORE_SUBMISSION, f"{CORE_ENGINE_MODULE}.score_submission"),
        (
            _VERSIONED_CHECKER_REGISTRY,
            f"{CHECKER_REGISTRY_MODULE}.VersionedCheckerRegistry",
        ),
        (
            _RULE_EXECUTION_PLAN_BUILDER,
            f"{EXECUTION_PLAN_MODULE}.RuleExecutionPlanBuilder",
        ),
        (
            _LEGACY_PAPER_ADAPTER,
            f"{PAPER_ADAPTER_MODULE}.LegacyPaperAdapter",
        ),
        (
            _LEGACY_RUBRIC_ADAPTER,
            f"{RUBRIC_ADAPTER_MODULE}.LegacyRubricAdapter",
        ),
        (
            _CORE_RUN_PERSISTENCE,
            f"{PERSISTENCE_MODULE}.CoreRunPersistence",
        ),
    ):
        _require(value, label)


def _mapping(value) -> dict:
    method = getattr(value, "to_mapping", None)
    assert callable(method), "Core adapters must return public immutable DTOs"
    mapped = method()
    assert isinstance(mapped, Mapping)
    return deepcopy(dict(mapped))


def _contains_value(value, needle: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_value(key, needle) or _contains_value(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_value(item, needle) for item in value)
    return str(value) == needle


# ---------------------------------------------------------------------------
# LegacyPaperAdapter


def _legacy_paper_inputs(*, database_suffix: str):
    paper = SimpleNamespace(
        id=f"paper-db-{database_suffix}",
        student_id="20260001",
        student_name="张三",
        title="基于机器学习的教学质量评价研究",
        department="计算机学院",
        major="软件工程",
        advisor="测试导师",
        file_name="representative.docx",
        file_path=f"/private/uploads/{database_suffix}/representative.docx",
        parsed_text_path=f"/private/parsed/{database_suffix}.json",
        parse_quality=Decimal("0.98"),
    )
    parsed = {
        "title": paper.title,
        "student_id": paper.student_id,
        "student_name": paper.student_name,
        "full_text": "第一章 研究背景\n本文说明研究目标。\n第二章 研究方法\n本文采用问卷调查。",
        "sections": [
            {
                "id": f"legacy-section-{database_suffix}-1",
                "title": "第一章 研究背景",
                "level": 1,
                "page_start": 1,
                "page_end": 1,
                "paragraphs": [
                    {
                        "id": f"legacy-paragraph-{database_suffix}-1",
                        "page": 1,
                        "text": "本文说明研究目标。",
                    }
                ],
            },
            {
                "id": f"legacy-section-{database_suffix}-2",
                "title": "第二章 研究方法",
                "level": 1,
                "page_start": 2,
                "page_end": 2,
                "paragraphs": [
                    {
                        "id": f"legacy-paragraph-{database_suffix}-2",
                        "page": 2,
                        "text": "本文采用问卷调查。",
                    }
                ],
            },
        ],
        "references": [],
        "structure_checks": [],
        "parse_quality": "0.98",
    }
    chunks = [
        SimpleNamespace(
            id=f"chunk-db-{database_suffix}-1",
            paper_id=paper.id,
            section_title="第一章 研究背景",
            page_start=1,
            page_end=1,
            paragraph_ids=[f"legacy-paragraph-{database_suffix}-1"],
            text="本文说明研究目标。",
        ),
        SimpleNamespace(
            id=f"chunk-db-{database_suffix}-2",
            paper_id=paper.id,
            section_title="第二章 研究方法",
            page_start=2,
            page_end=2,
            paragraph_ids=[f"legacy-paragraph-{database_suffix}-2"],
            text="本文采用问卷调查。",
        ),
    ]
    return paper, parsed, chunks


def _adapt_legacy_paper(
    *,
    suffix: str,
    source_artifact_hash: str = "a" * 64,
    first_paragraph: str | None = None,
):
    adapter_type = _require(
        _LEGACY_PAPER_ADAPTER,
        f"{PAPER_ADAPTER_MODULE}.LegacyPaperAdapter",
    )
    paper, parsed, chunks = _legacy_paper_inputs(database_suffix=suffix)
    if first_paragraph is not None:
        old_text = parsed["sections"][0]["paragraphs"][0]["text"]
        parsed["sections"][0]["paragraphs"][0]["text"] = first_paragraph
        parsed["full_text"] = parsed["full_text"].replace(old_text, first_paragraph)
        chunks[0].text = first_paragraph
    return adapter_type().adapt(
        paper=paper,
        parsed=parsed,
        chunks=chunks,
        source_artifact_hash=source_artifact_hash,
        profile_key="thesis",
        profile_version="thesis-legacy-profile@1",
        parser_version="legacy-document-parser@1",
        normalizer_version="legacy-normalizer@1",
    )


@requires_paper_adapter
def test_legacy_paper_adapter_builds_stable_content_addressed_core_snapshots(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("adapter must not reopen an artifact when its hash is supplied")

    monkeypatch.setattr(builtins, "open", forbidden)
    first = _adapt_legacy_paper(suffix="one")
    second = _adapt_legacy_paper(suffix="two")

    assert isinstance(first.submission, SubmissionSnapshot)
    assert isinstance(first.document, DocumentSnapshot)
    first_submission = _mapping(first.submission)
    first_document = _mapping(first.document)
    assert first_submission == _mapping(second.submission)
    assert first_document == _mapping(second.document)
    assert first_submission["source_artifact_hash"] == "a" * 64
    assert first_document["profile_key"] == "thesis"
    assert first_document["document_snapshot_hash"] == canonical_sha256(
        document_snapshot_projection(first_document)
    )
    assert len({item["evidence_unit_id"] for item in first_document["evidence_units"]}) == len(
        first_document["evidence_units"]
    )
    for forbidden_value in (
        "paper-db-one",
        "chunk-db-one-1",
        "/private/uploads/one/representative.docx",
        "/private/parsed/one.json",
        "legacy-paragraph-one-1",
    ):
        assert not _contains_value(first_submission, forbidden_value)
        assert not _contains_value(first_document, forbidden_value)


@requires_paper_adapter
def test_legacy_paper_identity_changes_only_for_authoritative_artifact_or_content():
    baseline = _adapt_legacy_paper(suffix="baseline")
    content_changed = _adapt_legacy_paper(
        suffix="content",
        first_paragraph="本文说明改变后的研究目标。",
    )
    artifact_changed = _adapt_legacy_paper(
        suffix="artifact",
        source_artifact_hash="b" * 64,
    )

    baseline_submission = _mapping(baseline.submission)
    baseline_document = _mapping(baseline.document)
    assert _mapping(content_changed.submission) == baseline_submission
    assert _mapping(content_changed.document)["content_hash"] != baseline_document[
        "content_hash"
    ]
    assert _mapping(content_changed.document)[
        "document_snapshot_hash"
    ] != baseline_document["document_snapshot_hash"]
    assert _mapping(artifact_changed.submission)["source_artifact_hash"] == "b" * 64
    assert _mapping(artifact_changed.document) == baseline_document


# ---------------------------------------------------------------------------
# LegacyRubricAdapter


def _legacy_rubric_inputs(*, suffix: str):
    rubric = {
        "id": f"rubric-db-{suffix}",
        "name": "技术方案评分标准",
        "version": "legacy-v1",
        "status": "published",
        "total_score": "100",
    }
    criteria = [
        {
            "id": f"criterion-db-{suffix}-risk",
            "code": "RISK_CONTROL",
            "name": "风险控制完整性",
            "max_score": "20",
            "weight": None,
            "criterion_type": "deterministic",
            "scoring_mode": "deductive",
            "description": "必须明确风险负责人。",
            "evidence_hints": ["风险控制"],
            "deduction_rules": ["缺少负责人扣10分"],
            "deduction_rules_structured": [
                {
                    "match": "required_field_missing:owner",
                    "points": "10",
                    "reason": "缺少风险负责人",
                    "source": "评分标准!A2",
                    "evidence_mode": "scoped_absence",
                    "absence_target": "risk_owner",
                }
            ],
            "rubric_levels": [],
            "sub_checks": [],
            "applies_to": "risk_control",
            "dimension": "content",
        },
        {
            "id": f"criterion-db-{suffix}-fit",
            "code": "SOLUTION_FIT",
            "name": "方案匹配度",
            "max_score": "80",
            "weight": None,
            "criterion_type": "llm_judgment",
            "scoring_mode": "banded",
            "description": "需求与方案应明确对应。",
            "evidence_hints": ["需求理解", "总体方案"],
            "deduction_rules": [],
            "deduction_rules_structured": [],
            "rubric_levels": [
                {"level_code": "FIT_HIGH", "points": "80", "descriptor": "明确对应"},
                {"level_code": "FIT_LOW", "points": "40", "descriptor": "部分对应"},
            ],
            "sub_checks": [],
            "applies_to": "requirements_understanding",
            "dimension": "content",
        },
    ]
    return rubric, criteria


def _adapt_legacy_rubric(*, suffix: str, compilation_rows=()):
    adapter_type = _require(
        _LEGACY_RUBRIC_ADAPTER,
        f"{RUBRIC_ADAPTER_MODULE}.LegacyRubricAdapter",
    )
    rubric, criteria = _legacy_rubric_inputs(suffix=suffix)
    return adapter_type().adapt(
        rubric=rubric,
        criteria=criteria,
        policy_snapshot=technical_policy_snapshot_payload(total_score="100"),
        business_profile_key="thesis",
        compilation_rows=list(compilation_rows),
    )


@requires_rubric_adapter
def test_legacy_rubric_adapter_produces_a_complete_stable_core_snapshot():
    first = _adapt_legacy_rubric(suffix="one")
    second = _adapt_legacy_rubric(suffix="two")

    assert isinstance(first, CompiledRubricSnapshot)
    first_mapping = _mapping(first)
    assert first_mapping == _mapping(second)
    assert first_mapping["rubric_source_kind"] == "legacy_unversioned"
    assert first_mapping["business_profile_key"] == "thesis"
    assert first_mapping["total_score"] == "100"
    assert "rubric_version_id" not in first_mapping
    assert "version_hash" not in first_mapping
    assert "hash_scheme" not in first_mapping
    assert {item["criterion_code"] for item in first_mapping["criteria"]} == {
        "RISK_CONTROL",
        "SOLUTION_FIT",
    }
    assert {item["criterion_code"] for item in first_mapping["atomic_rules"]} == {
        "RISK_CONTROL",
        "SOLUTION_FIT",
    }
    assert first_mapping["global_policy"]["policy_hash"]
    rubric_projection = deepcopy(first_mapping)
    supplied_hash = rubric_projection.pop("rubric_snapshot_hash")
    assert supplied_hash == canonical_sha256(
        {"scheme": "compiled-rubric-snapshot-v1", **rubric_projection}
    )
    assert "rubric-db-one" not in repr(first_mapping)
    assert "criterion-db-one" not in repr(first_mapping)


@requires_rubric_adapter
def test_legacy_rubric_adapter_refuses_to_hide_incomplete_formal_provenance():
    with pytest.raises((TypeError, ValueError), match=r"(?i)(provenance|version|compilation|legacy)"):
        _adapt_legacy_rubric(
            suffix="provenance",
            compilation_rows=[{"id": "compilation-without-published-version"}],
        )


# ---------------------------------------------------------------------------
# CoreRunPersistence


class _InMemoryDocumentSnapshotStore:
    """Test port proving that a replay ref resolves to verified immutable content."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.resolve_calls: list[str] = []

    def put(self, snapshot) -> str:
        mapping = _mapping(snapshot) if not isinstance(snapshot, Mapping) else deepcopy(dict(snapshot))
        snapshot_hash = mapping["document_snapshot_hash"]
        ref = f"memory://document-snapshots/sha256/{snapshot_hash}.json"
        self.objects[ref] = mapping
        return ref

    def resolve(self, *, ref: str):
        self.resolve_calls.append(ref)
        if ref not in self.objects:
            raise KeyError(f"document snapshot ref does not exist: {ref}")
        return deepcopy(self.objects[ref])


def _outcome_for(request: dict) -> ScoringOutcome:
    return ScoringOutcome.from_mapping(
        {
            "schema_version": "scoring-outcome@1",
            "request_identity": {
                "idempotency_key": request["idempotency_key"],
                "document_snapshot_hash": request["document"]["document_snapshot_hash"],
                "rubric_snapshot_hash": request["plan"]["rubric_snapshot_hash"],
                "plan_hash": request["plan"]["plan_hash"],
                "policy_hash": request["plan"]["policy_hash"],
                "profile_key": request["submission"]["profile_key"],
            },
            "criterion_outcomes": [
                {
                    "criterion_code": "RISK_CONTROL",
                    "status": "calculated",
                    "auto_score": "10",
                    "final_score": "10",
                    "max_score": "20",
                },
                {
                    "criterion_code": "SOLUTION_FIT",
                    "status": "calculated",
                    "auto_score": "80",
                    "final_score": "80",
                    "max_score": "80",
                },
            ],
            "rule_decisions": [
                {
                    "rule_code": DETERMINISTIC_RULE_CODE,
                    "status": "triggered",
                    "evidence_refs": [],
                },
                {
                    "rule_code": SEMANTIC_RULE_CODE,
                    "status": "triggered",
                    "evidence_refs": [],
                },
            ],
            "score_contributions": [
                {
                    "criterion_code": "RISK_CONTROL",
                    "rule_code": None,
                    "kind": "base",
                    "amount": "20",
                },
                {
                    "criterion_code": "RISK_CONTROL",
                    "rule_code": DETERMINISTIC_RULE_CODE,
                    "kind": "deduction",
                    "amount": "-10",
                },
                {
                    "criterion_code": "SOLUTION_FIT",
                    "rule_code": SEMANTIC_RULE_CODE,
                    "kind": "band",
                    "amount": "80",
                },
            ],
            "review_issues": [],
            "unrounded_total": "90",
            "final_total": "90",
            "grade": "A",
            "status": "completed",
            "audit_identity": deepcopy(request["runtime_identity"]),
        }
    )


def _persistence_db():
    request = scoring_request_payload()
    plan = request["plan"]
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user = models.User(
        id="user-m3-persistence",
        username="m3-persistence",
        display_name="M3 persistence fixture",
        role="reviewer",
    )
    rubric = models.Rubric(
        id="rubric-m3-persistence",
        name="M3 persistence rubric",
        version="1",
        total_score=100,
        status="published",
        created_by=user.id,
        published_at=now,
    )
    criteria = [
        models.RubricCriterion(
            id="criterion-risk",
            rubric_id=rubric.id,
            code="RISK_CONTROL",
            name="Risk control",
            max_score=20,
            criterion_type="deterministic",
            scoring_mode="deductive",
        ),
        models.RubricCriterion(
            id="criterion-fit",
            rubric_id=rubric.id,
            code="SOLUTION_FIT",
            name="Solution fit",
            max_score=80,
            criterion_type="llm_judgment",
            scoring_mode="banded",
        ),
    ]
    compilation = models.RubricCompilation(
        id="compilation-m3-persistence",
        rubric_id=rubric.id,
        status="validated",
        parser_version="technical-proposal-rubric-parser@1",
        compiler_version="technical-proposal-rubric-compiler@1",
        prompt_version="technical-proposal-rubric-prompt@1",
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
        version="1.0.0",
        workflow_profile="template_driven",
        global_policy=deepcopy(plan["policy_snapshot"]),
        version_hash=plan["rubric_version_hash"],
        business_profile_key=plan["business_profile_key"],
        hash_scheme=plan["rubric_hash_scheme"],
        created_by=user.id,
    )
    batch = models.GradingBatch(
        id="batch-m3-persistence",
        name="M3 persistence batch",
        rubric_id=rubric.id,
        rubric_version_id=version.id,
        created_by=user.id,
    )
    paper = models.Paper(
        id="paper-m3-persistence",
        batch_id=batch.id,
        file_name="memory.docx",
        file_path="memory://paper.docx",
        status="parsed",
    )
    db.add_all([user, rubric, *criteria, compilation, version, batch, paper])
    db.commit()
    assert db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    assert db.get(models.RubricVersion, plan["rubric_version_id"]) is not None
    return engine, db, rubric, criteria, paper


_AUTO_DOCUMENT_REF = object()


def _persist(
    db,
    rubric,
    criteria,
    paper,
    request,
    *,
    snapshot_store: _InMemoryDocumentSnapshotStore | None = None,
    document_snapshot_ref=_AUTO_DOCUMENT_REF,
):
    persistence_type = _require(
        _CORE_RUN_PERSISTENCE,
        f"{PERSISTENCE_MODULE}.CoreRunPersistence",
    )
    store = snapshot_store or _InMemoryDocumentSnapshotStore()
    if document_snapshot_ref is _AUTO_DOCUMENT_REF:
        document_snapshot_ref = store.put(request["document"])
    return persistence_type(db, document_snapshot_store=store).persist(
        request=deepcopy(request),
        outcome=_outcome_for(request),
        paper_id=paper.id,
        rubric_id=rubric.id,
        criterion_id_by_code={item.code: item.id for item in criteria},
        workflow_profile="template_driven",
        document_snapshot_ref=document_snapshot_ref,
    )


@requires_persistence
def test_core_persistence_reuses_authoritative_run_and_rescore_generation_creates_new_run():
    _require(_CORE_RUN_PERSISTENCE, f"{PERSISTENCE_MODULE}.CoreRunPersistence")
    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        store = _InMemoryDocumentSnapshotStore()
        ref = store.put(request["document"])
        first = _persist(
            db,
            rubric,
            criteria,
            paper,
            request,
            snapshot_store=store,
            document_snapshot_ref=ref,
        )
        duplicate = _persist(
            db,
            rubric,
            criteria,
            paper,
            request,
            snapshot_store=store,
            document_snapshot_ref=ref,
        )
        assert duplicate.id == first.id
        assert db.scalar(select(func.count()).select_from(models.ScoringRun)) == 1
        assert db.scalar(select(func.count()).select_from(models.ScoreItem)) == 2
        assert store.resolve_calls
        assert set(store.resolve_calls) == {ref}

        rescore = scoring_request_payload(rescore_generation=1)
        second = _persist(
            db,
            rubric,
            criteria,
            paper,
            rescore,
            snapshot_store=store,
            document_snapshot_ref=ref,
        )
        assert second.id != first.id
        assert second.rescore_generation == 1
        assert db.scalar(select(func.count()).select_from(models.ScoringRun)) == 2
        assert db.scalar(select(func.count()).select_from(models.ScoreItem)) == 4
    finally:
        db.close()
        engine.dispose()


def _refresh_policy_hash(policy: dict) -> None:
    projection = deepcopy(policy)
    projection.pop("policy_hash", None)
    policy["policy_hash"] = canonical_sha256(projection)


def _mutate_collision_identity(request: dict, identity_part: str) -> None:
    if identity_part == "source-artifact":
        request["submission"]["source_artifact_hash"] = "b" * 64
        request["submission"]["artifact_refs"][0].update(
            {"ref": "blob:sha256:" + "b" * 64, "content_hash": "b" * 64}
        )
    elif identity_part == "document":
        request["document"]["parser_version"] = "technical-proposal-parser@2"
        request["document"]["document_snapshot_hash"] = canonical_sha256(
            document_snapshot_projection(request["document"])
        )
    elif identity_part == "rubric-snapshot":
        request["plan"]["rubric_snapshot_hash"] = "e" * 64
        request["plan"]["plan_hash"] = canonical_sha256(
            plan_hash_projection(request["plan"])
        )
    elif identity_part == "rubric-version":
        request["plan"]["rubric_version_hash"] = "e" * 64
        request["plan"]["plan_hash"] = canonical_sha256(
            plan_hash_projection(request["plan"])
        )
    elif identity_part == "execution-plan":
        request["plan"]["engine_contract_version"] = "scoring-core@1.0.1"
        request["runtime_identity"]["engine_contract_version"] = "scoring-core@1.0.1"
        request["plan"]["plan_hash"] = canonical_sha256(
            plan_hash_projection(request["plan"])
        )
    elif identity_part == "policy":
        request["plan"]["policy_snapshot"]["rounding"]["digits"] = 1
        _refresh_policy_hash(request["plan"]["policy_snapshot"])
        request["plan"]["policy_hash"] = request["plan"]["policy_snapshot"][
            "policy_hash"
        ]
        request["plan"]["plan_hash"] = canonical_sha256(
            plan_hash_projection(request["plan"])
        )
    elif identity_part == "profile":
        for value in (
            request["submission"],
            request["document"],
            request["runtime_identity"],
        ):
            value["profile_key"] = "technical_proposal_variant"
        request["plan"]["business_profile_key"] = "technical_proposal_variant"
        request["plan"]["checker_manifest"][
            "technical_proposal.required_fields.v1"
        ]["supported_profiles"] = ["technical_proposal_variant"]
        request["document"]["document_snapshot_hash"] = canonical_sha256(
            document_snapshot_projection(request["document"])
        )
        request["plan"]["plan_hash"] = canonical_sha256(
            plan_hash_projection(request["plan"])
        )
    elif identity_part == "runtime":
        request["runtime_identity"]["provider"]["artifact_hash"] = "7" * 64
    else:
        request["rescore_generation"] = 1


@requires_persistence
@pytest.mark.parametrize(
    "identity_part",
    (
        "source-artifact",
        "document",
        "rubric-snapshot",
        "rubric-version",
        "execution-plan",
        "policy",
        "profile",
        "runtime",
        "rescore-generation",
    ),
)
def test_core_persistence_rejects_same_key_when_any_complete_identity_differs(
    identity_part,
):
    _require(_CORE_RUN_PERSISTENCE, f"{PERSISTENCE_MODULE}.CoreRunPersistence")
    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        _persist(db, rubric, criteria, paper, request)
        collision = deepcopy(request)
        _mutate_collision_identity(collision, identity_part)
        # Deliberately preserve the occupied key: this is a collision test,
        # not a malformed canonical request test.
        collision["idempotency_key"] = request["idempotency_key"]
        with pytest.raises((TypeError, ValueError), match=r"(?i)(identity|idempot|collision)"):
            _persist(db, rubric, criteria, paper, collision)
        assert db.scalar(select(func.count()).select_from(models.ScoringRun)) == 1
    finally:
        db.close()
        engine.dispose()


@requires_persistence
@pytest.mark.parametrize("failure", ("missing-ref", "isolated-hash", "tampered-object"))
def test_core_persistence_requires_resolvable_hash_verified_document_snapshot(
    failure,
):
    _require(_CORE_RUN_PERSISTENCE, f"{PERSISTENCE_MODULE}.CoreRunPersistence")
    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        store = _InMemoryDocumentSnapshotStore()
        valid_ref = store.put(request["document"])
        if failure == "missing-ref":
            ref = valid_ref.replace(".json", "-missing.json")
        elif failure == "isolated-hash":
            ref = None
        else:
            ref = valid_ref
            # Keep the claimed hash string while changing authoritative
            # content.  Comparing only two stored strings must not pass.
            store.objects[ref]["parser_version"] = "tampered-parser@9"

        with pytest.raises((KeyError, TypeError, ValueError), match=r"(?i)(document|snapshot|ref|hash)"):
            _persist(
                db,
                rubric,
                criteria,
                paper,
                request,
                snapshot_store=store,
                document_snapshot_ref=ref,
            )
        assert db.scalar(select(func.count()).select_from(models.ScoringRun)) == 0
        assert db.scalar(select(func.count()).select_from(models.ScoreItem)) == 0
    finally:
        db.close()
        engine.dispose()


@requires_persistence
def test_core_persistence_writes_complete_replay_identity_and_per_rule_results():
    _require(_CORE_RUN_PERSISTENCE, f"{PERSISTENCE_MODULE}.CoreRunPersistence")
    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        store = _InMemoryDocumentSnapshotStore()
        ref = store.put(request["document"])
        run = _persist(
            db,
            rubric,
            criteria,
            paper,
            request,
            snapshot_store=store,
            document_snapshot_ref=ref,
        )
        plan = request["plan"]
        document = request["document"]
        submission = request["submission"]
        runtime = request["runtime_identity"]
        assert run.rubric_source_kind == plan["rubric_source_kind"]
        assert run.rubric_snapshot_hash == plan["rubric_snapshot_hash"]
        assert run.rubric_version_id == plan["rubric_version_id"]
        assert run.rubric_version_hash == plan["rubric_version_hash"]
        assert run.rubric_hash_scheme == plan["rubric_hash_scheme"]
        assert run.business_profile_key == submission["profile_key"]
        assert run.workflow_profile == "template_driven"
        assert run.execution_plan_snapshot == plan
        assert run.execution_plan_hash == plan["plan_hash"]
        assert run.plan_schema_version == plan["schema_version"]
        assert run.checker_manifest == plan["checker_manifest"]
        assert run.policy_snapshot == plan["policy_snapshot"]
        assert run.policy_hash == plan["policy_hash"]
        assert run.policy_schema_version == plan["policy_snapshot"]["schema_version"]
        assert run.source_artifact_hash == submission["source_artifact_hash"]
        assert run.normalized_content_hash == document["content_hash"]
        assert run.document_snapshot_hash == document["document_snapshot_hash"]
        assert run.document_schema_version == document["schema_version"]
        assert run.document_snapshot_ref == ref
        assert store.resolve_calls
        assert set(store.resolve_calls) == {ref}
        assert store.resolve(ref=run.document_snapshot_ref)[
            "document_snapshot_hash"
        ] == run.document_snapshot_hash
        assert run.engine_version == runtime["engine_version"]
        assert run.model_provider == runtime["provider"]["name"]
        assert run.model_name == runtime["provider"]["model"]
        assert run.model_version == runtime["provider"]["model_version"]
        assert run.rescore_generation == 0
        assert run.idempotency_key == request["idempotency_key"]
        assert Decimal(str(run.ai_total_score)) == Decimal("90")
        assert Decimal(str(run.final_total_score)) == Decimal("90")
        assert run.grade == "A"

        items = db.scalars(
            select(models.ScoreItem).where(models.ScoreItem.scoring_run_id == run.id)
        ).all()
        assert len(items) == 2
        for item in items:
            assert item.rule_results_schema_version == "rule-results@1"
            assert item.rule_results
            assert item.aggregation
            assert item.aggregation_schema_version
            assert item.auto_score_status == "calculated"
        serialized_rules = repr([item.rule_results for item in items])
        assert DETERMINISTIC_RULE_CODE in serialized_rules
        assert SEMANTIC_RULE_CODE in serialized_rules
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Rollout modes and public compatibility


@requires_modes
def test_scoring_engine_mode_defaults_to_legacy_and_rejects_unknown_values():
    _require_mode_setting()
    field = Settings.model_fields["SCORING_ENGINE_MODE"]
    assert field.default == "legacy"
    for mode in ("legacy", "compare", "core"):
        assert Settings(SCORING_ENGINE_MODE=mode).SCORING_ENGINE_MODE == mode
    with pytest.raises(ValidationError):
        Settings(SCORING_ENGINE_MODE="shadow-write-official")


def test_score_paper_signature_and_papers_routes_remain_backward_compatible():
    from backend.app.services.scoring.engine import score_paper

    signature = inspect.signature(score_paper)
    assert list(signature.parameters) == ["db", "paper_id", "scorer"]
    assert signature.parameters["scorer"].default is None
    routes = {
        (route.path, frozenset(getattr(route, "methods", None) or ()))
        for route in app.routes
    }
    assert ("/api/papers", frozenset({"GET"})) in routes
    assert ("/api/papers/{paper_id}/score", frozenset({"POST"})) in routes


def _upload_paper(client, batch_id: str, *, suffix: str):
    document = make_sample_docx()
    response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={
            "file": (
                f"m3-mode-{suffix}.docx",
                document.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _business_projection(run: dict, items: list[dict]):
    def without_paper_local_chunk_identity(value):
        """Compare scoring semantics, not per-paper retrieval row identities.

        ``chunk_id`` remains in the public evidence contract and must identify
        a chunk belonging to the current paper (covered by API tests).  Two
        byte-identical uploads necessarily have different PaperChunk primary
        keys, so those local locators are not a cross-paper business-parity
        field for legacy-vs-compare rollout assertions.
        """

        if isinstance(value, Mapping):
            return {
                key: without_paper_local_chunk_identity(item)
                for key, item in value.items()
                if key != "chunk_id"
            }
        if isinstance(value, list):
            return [without_paper_local_chunk_identity(item) for item in value]
        return deepcopy(value)

    return {
        "run": {
            key: run[key]
            for key in (
                "status",
                "model_provider",
                "model_name",
                "model_version",
                "ai_total_score",
                "final_total_score",
                "grade",
                "need_manual_review",
            )
        },
        "items": without_paper_local_chunk_identity([
            {
                key: item[key]
                for key in (
                    "criterion_code",
                    "max_score",
                    "ai_score",
                    "final_score",
                    "evidence_sufficient",
                    "reason",
                    "deductions",
                    "deduction_items",
                    "evidence",
                    "band_selection",
                    "sub_results",
                    "need_manual_review",
                )
            }
            for item in items
        ]),
    }


M3_CORE_ROUTE_RUBRIC = {
    "name": "M3 core mode minimal vertical rubric",
    "version": "legacy-core-v1",
    "description": "Exactly one deterministic deduct and one semantic band.",
    "total_score": 100,
    "criteria": [
        {
            "code": "RISK_CONTROL",
            "name": "Risk control completeness",
            "max_score": 20,
            "criterion_type": "deterministic",
            "scoring_mode": "deductive",
            "applies_to": "risk_control",
            "dimension": "content",
            "description": "A risk owner is required.",
            "evidence_hints": ["风险控制", "负责人"],
            "deduction_rules": ["Missing owner deducts 10 points."],
            "deduction_rules_structured": [
                {
                    "match": "required_field_missing:owner",
                    "points": "10",
                    "reason": "Risk owner is missing.",
                    "source": "M3 core route fixture",
                    "evidence_mode": "scoped_absence",
                    "absence_target": "risk_owner",
                }
            ],
            "display_order": 1,
        },
        {
            "code": "SOLUTION_FIT",
            "name": "Solution fit",
            "max_score": 80,
            "criterion_type": "llm_judgment",
            "scoring_mode": "banded",
            "applies_to": "requirements_understanding",
            "dimension": "content",
            "description": "Requirements and solution must be aligned.",
            "evidence_hints": ["需求理解", "总体方案"],
            "rubric_levels": [
                {
                    "level_code": "FIT_HIGH",
                    "points": "80",
                    "descriptor": "Requirements and solution are explicitly aligned.",
                },
                {
                    "level_code": "FIT_LOW",
                    "points": "40",
                    "descriptor": "The solution only partially addresses requirements.",
                },
            ],
            "display_order": 2,
        },
    ],
}


@pytest.fixture()
def m3_core_mode_client(tmp_path, monkeypatch):
    """Expose the route and its SQLite session for authoritative DB assertions."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path / "storage")
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_MOCK", True)
    ensure_storage_dirs()

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client, session_factory
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override
        engine.dispose()


def _core_route_stub_outcome(request_value) -> ScoringOutcome:
    request = (
        deepcopy(dict(request_value))
        if isinstance(request_value, Mapping)
        else _mapping(request_value)
    )
    nodes = request["plan"]["nodes"]
    assert len(nodes) == 2, "M3 core route fixture must compile exactly two nodes"
    by_criterion = {node["criterion_code"]: node for node in nodes}
    assert set(by_criterion) == {"RISK_CONTROL", "SOLUTION_FIT"}
    risk_rule = by_criterion["RISK_CONTROL"]["rule_code"]
    fit_rule = by_criterion["SOLUTION_FIT"]["rule_code"]
    return ScoringOutcome.from_mapping(
        {
            "schema_version": "scoring-outcome@1",
            "request_identity": {
                "idempotency_key": request["idempotency_key"],
                "document_snapshot_hash": request["document"][
                    "document_snapshot_hash"
                ],
                "rubric_snapshot_hash": request["plan"]["rubric_snapshot_hash"],
                "plan_hash": request["plan"]["plan_hash"],
                "policy_hash": request["plan"]["policy_hash"],
                "profile_key": request["submission"]["profile_key"],
            },
            "criterion_outcomes": [
                {
                    "criterion_code": "RISK_CONTROL",
                    "status": "calculated",
                    "auto_score": "10",
                    "final_score": "10",
                    "max_score": "20",
                },
                {
                    "criterion_code": "SOLUTION_FIT",
                    "status": "calculated",
                    "auto_score": "80",
                    "final_score": "80",
                    "max_score": "80",
                },
            ],
            "rule_decisions": [
                {
                    "rule_code": risk_rule,
                    "status": "triggered",
                    "evidence_refs": [],
                },
                {
                    "rule_code": fit_rule,
                    "status": "triggered",
                    "evidence_refs": [],
                },
            ],
            "score_contributions": [
                {
                    "criterion_code": "RISK_CONTROL",
                    "rule_code": None,
                    "kind": "base",
                    "amount": "20",
                },
                {
                    "criterion_code": "RISK_CONTROL",
                    "rule_code": risk_rule,
                    "kind": "deduction",
                    "amount": "-10",
                },
                {
                    "criterion_code": "SOLUTION_FIT",
                    "rule_code": fit_rule,
                    "kind": "band",
                    "amount": "80",
                },
            ],
            "review_issues": [],
            "unrounded_total": "90",
            "final_total": "90",
            "grade": "A",
            "status": "completed",
            "audit_identity": deepcopy(request["runtime_identity"]),
        }
    )


@requires_core_mode
def test_core_mode_route_persists_one_complete_authoritative_vertical_run(
    m3_core_mode_client,
    monkeypatch,
):
    """Core-mode route wiring, deliberately isolated from the unit-scored algorithm."""

    _require_core_mode_capabilities()
    client, session_factory = m3_core_mode_client
    core_module = importlib.import_module(CORE_ENGINE_MODULE)
    legacy_engine = importlib.import_module("backend.app.services.scoring.engine")
    score_submission = _SCORE_SUBMISSION
    calls: list[dict] = []

    def isolated_score_submission(*args, **kwargs):
        request_value = kwargs.get("request")
        if request_value is None:
            assert args, "score_submission must receive a ScoringRequest"
            request_value = args[0]
        request = (
            deepcopy(dict(request_value))
            if isinstance(request_value, Mapping)
            else _mapping(request_value)
        )
        calls.append(request)
        return _core_route_stub_outcome(request)

    # This is the only production behavior replaced: pure scoring itself is
    # covered in test_m3_score_submission.py.  Adapters, plan construction,
    # document snapshot storage and CoreRunPersistence remain real here.
    monkeypatch.setattr(core_module, "score_submission", isolated_score_submission)
    if vars(legacy_engine).get("score_submission") is score_submission:
        monkeypatch.setattr(
            legacy_engine,
            "score_submission",
            isolated_score_submission,
        )
    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "core")

    rubric_response = client.post("/api/rubrics", json=M3_CORE_ROUTE_RUBRIC)
    assert rubric_response.status_code == 200, rubric_response.text
    rubric_id = rubric_response.json()["id"]
    publish_response = client.post(f"/api/rubrics/{rubric_id}/publish")
    assert publish_response.status_code == 200, publish_response.text
    batch_response = client.post(
        "/api/batches",
        json={
            "name": "M3 isolated core-mode batch",
            "rubric_id": rubric_id,
        },
    )
    assert batch_response.status_code == 200, batch_response.text
    paper_id = _upload_paper(
        client,
        batch_response.json()["id"],
        suffix="core-authoritative",
    )

    score_response = client.post(f"/api/papers/{paper_id}/score")
    assert score_response.status_code == 200, score_response.text
    assert len(calls) == 1
    request = calls[0]
    assert request["plan"]["rubric_source_kind"] == "legacy_unversioned"
    assert request["submission"]["profile_key"] == "thesis"
    assert request["document"]["profile_key"] == "thesis"
    assert request["plan"]["business_profile_key"] == "thesis"
    assert {node["criterion_code"] for node in request["plan"]["nodes"]} == {
        "RISK_CONTROL",
        "SOLUTION_FIT",
    }

    with session_factory() as db:
        runs = db.scalars(
            select(models.ScoringRun).where(models.ScoringRun.paper_id == paper_id)
        ).all()
        assert len(runs) == 1
        run = runs[0]
        assert score_response.json()["id"] == run.id
        plan = request["plan"]
        submission = request["submission"]
        document = request["document"]
        runtime = request["runtime_identity"]
        assert run.rubric_source_kind == "legacy_unversioned"
        assert run.rubric_snapshot_hash == plan["rubric_snapshot_hash"]
        assert run.rubric_version_id is None
        assert run.rubric_version_hash is None
        assert run.rubric_hash_scheme is None
        assert run.business_profile_key == "thesis"
        assert isinstance(run.workflow_profile, str) and run.workflow_profile
        assert run.policy_snapshot == plan["policy_snapshot"]
        assert run.policy_hash == plan["policy_hash"]
        assert run.policy_schema_version == plan["policy_snapshot"][
            "schema_version"
        ]
        assert run.execution_plan_snapshot == plan
        assert run.execution_plan_hash == plan["plan_hash"]
        assert run.plan_schema_version == plan["schema_version"]
        assert run.checker_manifest == plan["checker_manifest"]
        assert run.source_artifact_hash == submission["source_artifact_hash"]
        assert run.normalized_content_hash == document["content_hash"]
        assert run.document_snapshot_hash == document["document_snapshot_hash"]
        assert run.document_schema_version == document["schema_version"]
        assert isinstance(run.document_snapshot_ref, str) and run.document_snapshot_ref
        assert run.engine_version == runtime["engine_version"]
        assert run.model_provider == runtime["provider"]["name"]
        assert run.model_name == runtime["provider"]["model"]
        assert run.model_version == runtime["provider"]["model_version"]
        assert run.rescore_generation == 0
        assert run.idempotency_key == request["idempotency_key"]
        assert Decimal(str(run.ai_total_score)) == Decimal("90")
        assert Decimal(str(run.final_total_score)) == Decimal("90")
        assert run.grade == "A"

        items = db.scalars(
            select(models.ScoreItem).where(models.ScoreItem.scoring_run_id == run.id)
        ).all()
        assert len(items) == 2
        item_codes = {db.get(models.RubricCriterion, item.criterion_id).code for item in items}
        assert item_codes == {"RISK_CONTROL", "SOLUTION_FIT"}
        for item in items:
            assert item.rule_results_schema_version == "rule-results@1"
            assert item.rule_results
            assert item.aggregation
            assert item.aggregation_schema_version
            assert item.auto_score_status == "calculated"
        serialized_rule_results = repr([item.rule_results for item in items])
        for node in plan["nodes"]:
            assert node["rule_code"] in serialized_rule_results


class _CapturingComparisonArtifactSink:
    def __init__(self):
        self.artifacts: list[dict] = []

    def record(self, *, artifact):
        assert isinstance(artifact, Mapping)
        self.artifacts.append(deepcopy(dict(artifact)))


def _comparison_stub_outcome(request) -> ScoringOutcome:
    """Return a non-scoring candidate without invoking a second model call.

    M3 exercises rollout wiring on a legacy thesis fixture before M5/M6 provide
    complete thesis Core parity.  The candidate is therefore review-only; this
    test must not make M3 execute unsupported M4 rules or double-call the LLM.
    """

    request = deepcopy(dict(request)) if isinstance(request, Mapping) else _mapping(request)
    return ScoringOutcome.from_mapping(
        {
            "schema_version": "scoring-outcome@1",
            "request_identity": {
                "idempotency_key": request["idempotency_key"],
                "document_snapshot_hash": request["document"][
                    "document_snapshot_hash"
                ],
                "rubric_snapshot_hash": request["plan"]["rubric_snapshot_hash"],
                "plan_hash": request["plan"]["plan_hash"],
                "policy_hash": request["plan"]["policy_hash"],
                "profile_key": request["submission"]["profile_key"],
            },
            "criterion_outcomes": [],
            "rule_decisions": [],
            "score_contributions": [],
            "review_issues": [
                {
                    "code": "M3_COMPARE_DECISION_REPLAY_REQUIRED",
                    "severity": "review",
                    "criterion_code": None,
                }
            ],
            "unrounded_total": None,
            "final_total": None,
            "grade": None,
            "status": "review_required",
            "audit_identity": deepcopy(request["runtime_identity"]),
        }
    )


@requires_compare
def test_compare_executes_core_once_but_keeps_legacy_as_the_only_official_artifact(
    client, monkeypatch
):
    _require_mode_setting()
    _require(
        _LEGACY_PAPER_ADAPTER,
        f"{PAPER_ADAPTER_MODULE}.LegacyPaperAdapter",
    )
    _require(
        _LEGACY_RUBRIC_ADAPTER,
        f"{RUBRIC_ADAPTER_MODULE}.LegacyRubricAdapter",
    )
    score_submission = _require(
        _SCORE_SUBMISSION,
        f"{CORE_ENGINE_MODULE}.score_submission",
    )
    _require(
        _COMPARISON_SINK,
        f"{COMPARISON_MODULE}.ComparisonArtifactSink",
    )
    get_comparison_sink = _require(
        _GET_COMPARISON_SINK,
        f"{COMPARISON_MODULE}.get_comparison_artifact_sink",
    )
    core_module = importlib.import_module(CORE_ENGINE_MODULE)
    legacy_engine = importlib.import_module("backend.app.services.scoring.engine")
    comparison_module = importlib.import_module(COMPARISON_MODULE)
    calls = []
    comparison_sink = _CapturingComparisonArtifactSink()

    def recorded_score_submission(*args, **kwargs):
        calls.append((args, kwargs))
        request = kwargs.get("request")
        if request is None:
            assert args, "score_submission must receive a ScoringRequest"
            request = args[0]
        return _comparison_stub_outcome(request)

    def captured_comparison_sink():
        return comparison_sink

    monkeypatch.setattr(core_module, "score_submission", recorded_score_submission)
    if vars(legacy_engine).get("score_submission") is score_submission:
        monkeypatch.setattr(legacy_engine, "score_submission", recorded_score_submission)
    monkeypatch.setattr(
        comparison_module,
        "get_comparison_artifact_sink",
        captured_comparison_sink,
    )
    if vars(legacy_engine).get("get_comparison_artifact_sink") is get_comparison_sink:
        monkeypatch.setattr(
            legacy_engine,
            "get_comparison_artifact_sink",
            captured_comparison_sink,
        )
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")

    rubric_response = client.post("/api/rubrics", json=M0_RUBRIC)
    assert rubric_response.status_code == 200, rubric_response.text
    batch_response = client.post(
        "/api/batches",
        json={
            "name": "M3 mode compatibility",
            "rubric_id": rubric_response.json()["id"],
        },
    )
    assert batch_response.status_code == 200, batch_response.text
    batch_id = batch_response.json()["id"]
    legacy_paper = _upload_paper(client, batch_id, suffix="legacy")
    compare_paper = _upload_paper(client, batch_id, suffix="compare")

    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "legacy")
    legacy_response = client.post(f"/api/papers/{legacy_paper}/score")
    assert legacy_response.status_code == 200, legacy_response.text
    assert calls == []

    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "compare")
    compare_response = client.post(f"/api/papers/{compare_paper}/score")
    assert compare_response.status_code == 200, compare_response.text
    assert len(calls) == 1
    candidate_request_value = calls[0][1].get("request") or calls[0][0][0]
    candidate_request = (
        deepcopy(dict(candidate_request_value))
        if isinstance(candidate_request_value, Mapping)
        else _mapping(candidate_request_value)
    )
    assert candidate_request["submission"]["profile_key"] == "thesis"
    assert candidate_request["document"]["profile_key"] == "thesis"
    assert candidate_request["plan"]["business_profile_key"] == "thesis"
    assert candidate_request["plan"]["rubric_source_kind"] == "legacy_unversioned"

    legacy_run = legacy_response.json()
    compare_run = compare_response.json()
    legacy_items = client.get(
        f"/api/scoring-runs/{legacy_run['id']}/items"
    ).json()
    compare_items = client.get(
        f"/api/scoring-runs/{compare_run['id']}/items"
    ).json()
    assert _business_projection(compare_run, compare_items) == _business_projection(
        legacy_run, legacy_items
    )
    official_runs = client.get(
        "/api/scoring-runs", params={"paper_id": compare_paper}
    )
    assert official_runs.status_code == 200
    assert [item["id"] for item in official_runs.json()] == [compare_run["id"]]

    assert len(comparison_sink.artifacts) == 1
    artifact = comparison_sink.artifacts[0]
    assert artifact["schema_version"] == "scoring-comparison-artifact@1"
    assert artifact["authority"] == "non_authoritative"
    assert artifact["legacy_run_id"] == compare_run["id"]
    assert artifact.get("candidate_run_id") is None
    assert artifact["candidate_identity"] == _comparison_stub_outcome(
        candidate_request
    ).to_mapping()["request_identity"]
    assert artifact["candidate_outcome"]["status"] == "review_required"
    assert artifact["candidate_outcome"]["final_total"] is None
    assert isinstance(artifact["diff"], Mapping)

    official_audit = client.get(f"/api/scoring-runs/{compare_run['id']}")
    assert official_audit.status_code == 200
    for core_only_field in (
        "execution_plan_snapshot",
        "execution_plan_hash",
        "checker_manifest",
        "document_snapshot_ref",
        "document_snapshot_hash",
        "idempotency_key",
    ):
        if core_only_field in official_audit.json():
            assert official_audit.json()[core_only_field] is None

    export = client.get(f"/api/scoring-runs/{compare_run['id']}/export.json")
    report = client.get(f"/api/scoring-runs/{compare_run['id']}/report")
    review_logs = client.get(
        f"/api/scoring-runs/{compare_run['id']}/review-logs"
    )
    assert export.status_code == report.status_code == review_logs.status_code == 200
    official_surfaces = (repr(export.json()) + report.text + repr(review_logs.json())).lower()
    for forbidden in ("core_outcome", "candidate_outcome", "comparison_artifact"):
        assert forbidden not in official_surfaces
    assert review_logs.json() == []
