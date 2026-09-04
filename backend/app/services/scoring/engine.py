from datetime import datetime
from datetime import timezone
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import fields
from dataclasses import is_dataclass
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from types import SimpleNamespace
import time

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.services.llm.base import LLMScoringError
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.ai_connections import resolve_connection_runtime
from backend.app.services.ai_connections import record_usage_ledger
from backend.app.services.ai_connections import validate_outbound_base_url
from backend.app.services.llm.debug_logging import log_llm_throttle_sleep
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.cache import llm_cache
from backend.app.services.calibration import get_anchors
from backend.app.services.checkers import run_deterministic_checker
from backend.app.services.checkers.deterministic import AUTHOR_YEAR_RE
from backend.app.services.checkers.deterministic import COMPLETENESS_CODES
from backend.app.services.checkers.deterministic import FIGURE_REF_RE
from backend.app.services.checkers.deterministic import INTEXT_NUM_RE
from backend.app.services.checkers.deterministic import WORD_COUNT_MIN_DEFAULT
from backend.app.services.checkers.findings_checker import is_findings_enabled
from backend.app.services.checkers.findings_checker import score_from_findings
from backend.app.services.coherence import analyze_semantic_coherence
from backend.app.services.document_parser.format_check import compare_format
from backend.app.services.document_parser.format_resolver import resolve_default_format
from backend.app.services.document_parser.chunking import build_chunks
from backend.app.services.retrieval.keyword import retrieve_for_criterion
from backend.app.services.retrieval.keyword import retrieve_for_criterion_in_chunks
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import RuleExecutionPlan, ScoringRequest
from backend.app.services.scoring.core.engine import score_submission
from backend.app.services.scoring.observed import score_submission_observed
from backend.app.services.scoring.core.identity import hash_source_artifact
from backend.app.services.scoring.core.identity import (
    scoring_request_idempotency_projection,
)
from backend.app.services.scoring.core.evidence import validate_evidence
from backend.app.services.scoring.core.execution_plan import RuleExecutionPlanBuilder
from backend.app.services.scoring.core.policy import AGGREGATION_SCHEMA_VERSION
from backend.app.services.scoring.core.policy import aggregate_scores
from backend.app.services.scoring.core.policy import build_corrected_thesis_policy
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.services.scoring.core.policy import validate_weight_configuration
from backend.app.services.scoring.rules import calculate_total_score
from backend.app.services.scoring.rules import match_grade
from backend.app.services.scoring.rules import need_manual_review
from backend.app.services.scoring.validator import validate_score_output
from backend.app.services.storage.local import materialize, read_json
from backend.app.services.scoring.adapters.comparison import (
    get_comparison_artifact_sink,
)
from backend.app.services.scoring.adapters.legacy_rubric import LegacyRubricAdapter
from backend.app.services.scoring.adapters.rubric_snapshot import (
    CompiledRubricSnapshotLoader,
)
from backend.app.services.scoring.adapters.persistence import (
    CoreRunPersistence,
    LocalDocumentSnapshotStore,
)
from backend.app.services.scoring.profiles.thesis import ThesisProfile


@dataclass
class CriterionPlan:
    """单个评分项的"计算计划"：评分项快照 + 预取的证据候选/校准锚点（compute 阶段不再碰 DB）。"""

    criterion: SimpleNamespace
    route: str  # findings | deterministic | hybrid | chunks
    candidates: list = field(default_factory=list)
    anchors: list = field(default_factory=list)
    sub_plans: list = field(default_factory=list)  # [(sub_criterion, candidates)]


@dataclass
class ScoringInputs:
    """评分所需的全部输入（已脱离 DB）；由 collect_scoring_inputs 一次性预取。"""

    paper_id: str
    paper_title: object
    parse_quality: object
    parsed: dict
    structure_checks: list
    rubric_id: str
    rubric_total_score: object
    rubric_version: object  # legacy Rubric.version 显示标签，不是 RubricVersion identity
    base_coherence: list
    format_findings: list
    criteria: list  # [CriterionPlan]
    authoritative: bool = False
    policy_snapshot: object = None
    rubric_snapshot: object = None


@dataclass
class ScoringResult:
    """compute_scoring 的纯输出；由 persist_scoring 落库为 ScoringRun + ScoreItem。"""

    items: list
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    coherence_findings: list
    format_findings: list
    ai_total: object
    final_total: object
    grade: object
    need_review: bool


def _close_scorer(scorer):
    """关闭 scorer 持有的资源（真实 provider 的 httpx.Client）；mock 无 close 则跳过。"""
    close = getattr(scorer, "close", None)
    if callable(close):
        close()


def _scorer_for_batch(db: Session, batch: GradingBatch):
    """Use a batch-pinned private connection or the legacy platform runtime."""

    if batch.ai_connection_id is None:
        return get_llm_scorer()
    if not batch.owner_id or not batch.organization_id:
        raise ValueError("BYOK batch has no owner or organization identity")
    runtime = resolve_connection_runtime(
        db,
        connection_id=batch.ai_connection_id,
        owner_id=batch.owner_id,
        organization_id=batch.organization_id,
    )
    if runtime.key_version != batch.ai_connection_key_version:
        raise ValueError("AI connection key has changed; recreate the scoring task")
    if batch.ai_connection_snapshot != runtime.snapshot():
        raise ValueError("AI connection configuration has changed; recreate the scoring task")
    validate_outbound_base_url(runtime.base_url)
    return get_llm_scorer(runtime)


def _scorer_for_paper(db: Session, paper_id: str):
    batch = db.scalar(
        select(GradingBatch).join(Paper, Paper.batch_id == GradingBatch.id).where(Paper.id == paper_id)
    )
    if batch is None:
        raise ValueError("paper not found")
    return _scorer_for_batch(db, batch)


def score_paper(db: Session, paper_id: str, scorer=None):
    """编排：预取(读) → 释放事务 → 纯计算(LLM，无锁) → 落库(短写)。

    把 LLM 调用移出 DB 事务，使 `--workers` 真正并行、避免 sqlite 写锁/快照串行；
    签名与返回（ScoringRun）保持不变，所有调用方（Web/CLI/eval/score_batch）无需改动。
    """
    owns_scorer = scorer is None  # 自建的 scorer 用完要关其 http client，避免连接池/fd 泄漏
    scorer = scorer or _scorer_for_paper(db, paper_id)
    try:
        mode = settings.SCORING_ENGINE_MODE
        if mode not in {"legacy", "compare", "core"}:
            # Settings validates this already; retaining a fail-closed runtime
            # check protects callers that mutate the singleton in-process.
            raise ValueError("unsupported scoring engine mode: %s" % mode)

        # A batch-locked RubricVersion is a formal M4 graph.  Such a graph must
        # never be projected back through the legacy arbitrary-points path,
        # even while the global rollout flag remains on ``legacy`` until M8.
        # Legacy/unversioned batches continue to obey SCORING_ENGINE_MODE.
        if _has_locked_formal_version(db, paper_id):
            return _score_paper_core(db, paper_id, scorer)
        if mode == "legacy":
            return _score_paper_legacy(db, paper_id, scorer)
        if mode == "compare":
            legacy_run = _score_paper_legacy(db, paper_id, scorer)
            _record_core_comparison(
                db=db,
                paper_id=paper_id,
                scorer=scorer,
                legacy_run=legacy_run,
            )
            return legacy_run
        if mode == "core":
            return _score_paper_core(db, paper_id, scorer)
    finally:
        if owns_scorer:
            _close_scorer(scorer)


def _has_locked_formal_version(db: Session, paper_id: str) -> bool:
    return (
        db.scalar(
            select(GradingBatch.rubric_version_id)
            .join(Paper, Paper.batch_id == GradingBatch.id)
            .where(Paper.id == paper_id)
        )
        is not None
    )


def retry_score_paper(db: Session, run_id: str, scorer=None):
    """Create the next immutable scoring generation for an existing run."""

    previous = db.get(ScoringRun, run_id)
    if previous is None:
        raise ValueError("scoring run not found")
    # Historical non-Core runs have no replay identity and retain their
    # original behavior: every invocation creates an independent legacy run.
    if previous.rubric_source_kind is None:
        return score_paper(db, previous.paper_id, scorer=scorer)

    owns_scorer = scorer is None
    scorer = scorer or _scorer_for_paper(db, previous.paper_id)
    try:
        maximum = db.scalar(
            select(func.max(ScoringRun.rescore_generation)).where(
                ScoringRun.paper_id == previous.paper_id,
                ScoringRun.rescore_generation.is_not(None),
            )
        )
        generation = int(maximum if maximum is not None else -1) + 1
        return _score_paper_core(
            db,
            previous.paper_id,
            scorer,
            rescore_generation=generation,
        )
    finally:
        if owns_scorer:
            _close_scorer(scorer)


def _load_scoreable_paper(db: Session, paper_id: str):
    paper = db.scalar(
        select(Paper)
        .where(Paper.id == paper_id)
        .options(selectinload(Paper.batch), selectinload(Paper.chunks))
    )
    if paper is None:
        raise ValueError("paper not found")
    if paper.status == "failed":
        raise ValueError("paper parse failed: %s" % (paper.error_message or "unknown"))
    if not paper.parsed_text_path:
        raise ValueError("paper has no parsed text")
    return paper


def _score_paper_legacy(db: Session, paper_id: str, scorer):
    paper = _load_scoreable_paper(db, paper_id)
    started_at = _utcnow()
    inputs = collect_scoring_inputs(db, paper)
    # 结束事务：纯计算阶段不持锁/快照（sqlite 并发关键，避免 BUSY_SNAPSHOT）。
    # 用 commit 而非 rollback —— 保留调用方在本会话里既有的未提交改动（如 eval 批量先建的 batch/paper）。
    db.commit()
    result = compute_scoring(inputs, scorer)
    return persist_scoring(db, paper_id, inputs, result, scorer, started_at)


def _legacy_plan_hash_projection(value):
    return {
        "scheme": value["hash_scheme"],
        "schema_version": value["schema_version"],
        "rubric_source_kind": value["rubric_source_kind"],
        "rubric_version_id": value["rubric_version_id"],
        "rubric_version_hash": value["rubric_version_hash"],
        "rubric_hash_scheme": value["rubric_hash_scheme"],
        "rubric_snapshot_hash": value["rubric_snapshot_hash"],
        "policy_hash": value["policy_hash"],
        "policy_compiler_version": value["policy_compiler_version"],
        "business_profile_key": value["business_profile_key"],
        "business_profile_version": value["business_profile_version"],
        "checker_manifest": value["checker_manifest"],
        "engine_contract_version": value["engine_contract_version"],
        "nodes": value["nodes"],
        "dependency_order": value["dependency_order"],
    }


def _build_legacy_execution_plan(
    *, rubric_snapshot, profile, registry, compatibility_nodes=()
):
    rubric = rubric_snapshot.to_mapping()
    criteria = {
        item["criterion_code"]: deepcopy(item) for item in rubric["criteria"]
    }
    available_manifest = _plain_contract(
        registry.manifest(
            profile_key=profile.profile_key,
            document_schema_version="document-snapshot@1",
        )
    )
    compatibility = [item.to_mapping() for item in compatibility_nodes]
    compatibility_criteria = {
        item["criterion_code"] for item in compatibility
    }
    used_manifest = {}
    rules = {}
    for raw_rule in rubric["atomic_rules"]:
        rule = deepcopy(raw_rule)
        if rule["criterion_code"] in compatibility_criteria:
            continue
        checker_key = rule["checker_key"]
        if rule["judge_type"] == "deterministic":
            entry = available_manifest.get(checker_key)
            if entry is None:
                raise KeyError("unknown legacy checker: %s" % checker_key)
            rule["checker_version"] = entry["checker_version"]
            registry.resolve(
                checker_key=checker_key,
                checker_version=rule["checker_version"],
                checker_params=deepcopy(rule["checker_params"]),
                profile_key=profile.profile_key,
                document_schema_version="document-snapshot@1",
            )
            used_manifest[checker_key] = deepcopy(entry)
        rules[rule["rule_code"]] = rule
    atomic_nodes = [
        {
            "node_kind": "atomic_rule",
            "criterion_code": rules[code]["criterion_code"],
            "rule_code": code,
            "criterion_snapshot": deepcopy(
                criteria[rules[code]["criterion_code"]]
            ),
            "atomic_rule_snapshot": deepcopy(rules[code]),
        }
        for code in sorted(rules)
    ]
    nodes = sorted(
        atomic_nodes + [deepcopy(item) for item in compatibility],
        key=lambda item: item["rule_code"],
    )
    order = [item["rule_code"] for item in nodes]
    plan = {
        "schema_version": (
            "rule-execution-plan@3" if compatibility else "rule-execution-plan@2"
        ),
        "hash_scheme": "rule-execution-plan-v1",
        "rubric_source_kind": "legacy_unversioned",
        "rubric_version_id": None,
        "rubric_version_hash": None,
        "rubric_hash_scheme": None,
        "business_profile_key": profile.profile_key,
        "business_profile_version": profile.profile_version,
        "rubric_snapshot_hash": rubric["rubric_snapshot_hash"],
        "policy_snapshot": deepcopy(rubric["global_policy"]),
        "policy_hash": rubric["global_policy"]["policy_hash"],
        "policy_compiler_version": "scoring-policy-compiler@1",
        "nodes": nodes,
        "dependency_order": order,
        "checker_manifest": {
            key: used_manifest[key] for key in sorted(used_manifest)
        },
        "engine_contract_version": "scoring-core@1",
    }
    plan["plan_hash"] = canonical_sha256(_legacy_plan_hash_projection(plan))
    return RuleExecutionPlan.from_mapping(plan)


def _request_idempotency_projection(value):
    return scoring_request_idempotency_projection(value)


def _published_core_version(*, db, paper, rubric, compilations):
    """Resolve formal provenance to one exact version or fail closed."""

    if not compilations:
        return None
    compilation_by_id = {item.id: item for item in compilations}
    locked_version_id = getattr(paper.batch, "rubric_version_id", None)
    if locked_version_id is not None:
        version = db.get(RubricVersion, locked_version_id)
        if version is None or version.rubric_id != rubric.id:
            raise ValueError("batch rubric version identity is invalid")
        compilation = compilation_by_id.get(version.compilation_id)
        if not (
            getattr(rubric, "status", None) == "published"
            and getattr(rubric, "published_at", None) is not None
            and compilation is not None
            and compilation.rubric_id == rubric.id
            and compilation.status == "validated"
            and compilation.published_at is not None
            and compilation.published_at == rubric.published_at
            and compilation.final_version_hash == version.version_hash
        ):
            raise ValueError(
                "batch-locked rubric version is not consistently published"
            )
        return version

    candidates = []
    for version in db.scalars(
        select(RubricVersion).where(RubricVersion.rubric_id == rubric.id)
    ).all():
        compilation = compilation_by_id.get(version.compilation_id)
        if (
            compilation is not None
            and getattr(rubric, "status", None) == "published"
            and getattr(rubric, "published_at", None) is not None
            and compilation.status == "validated"
            and compilation.published_at is not None
            and compilation.published_at == rubric.published_at
            and compilation.final_version_hash == version.version_hash
        ):
            candidates.append(version)
    if len(candidates) != 1:
        raise ValueError(
            "formal rubric requires a batch-locked or unique published version"
        )
    return candidates[0]


def _core_request_context(db, paper_id, scorer, *, rescore_generation=0):
    paper = _load_scoreable_paper(db, paper_id)
    rubric = paper.batch.rubric
    parsed = read_json(paper.parsed_text_path)
    artifact_path = materialize(paper.file_path)
    if not artifact_path.is_file():
        raise ValueError("paper source artifact is unavailable for Core replay")
    source_artifact_hash = hash_source_artifact(artifact_path.read_bytes())
    profile = ThesisProfile()
    paper_snapshots = profile.adapt_paper(
        paper=paper,
        parsed=parsed,
        chunks=list(paper.chunks),
        source_artifact_hash=source_artifact_hash,
        submission_instance_key=paper.id,
    )
    compilations = list(
        db.scalars(
            select(RubricCompilation).where(
                RubricCompilation.rubric_id == rubric.id
            )
        ).all()
    )
    registry = profile.build_checker_registry()
    formal_version = _published_core_version(
        db=db,
        paper=paper,
        rubric=rubric,
        compilations=compilations,
    )
    if formal_version is not None:
        rubric_snapshot = CompiledRubricSnapshotLoader().load_from_session(
            session=db,
            rubric_version_id=formal_version.id,
            expected_profile_key=profile.profile_key,
        )
        plan = RuleExecutionPlanBuilder(
            checker_registry=registry,
            policy_compiler_version="scoring-policy-compiler@1",
            engine_contract_version="scoring-core@1",
        ).build(
            rubric=rubric_snapshot,
            profile=profile,
            document_schema_version="document-snapshot@1",
        )
        workflow_profile = formal_version.workflow_profile
    else:
        weight_validation = validate_weight_configuration(
            rubric.criteria,
            total_score=rubric.total_score,
        )
        policy = build_corrected_thesis_policy(
            rubric.total_score,
            weight_validation.mode,
        )
        rubric_projection = {
            "name": rubric.name,
            "status": "published",
            "total_score": rubric.total_score,
        }
        legacy_adapter = LegacyRubricAdapter()
        raw_criteria = list(rubric.criteria)
        rubric_snapshot = legacy_adapter.adapt(
            rubric=rubric_projection,
            criteria=raw_criteria,
            policy_snapshot=_plain_contract(policy),
            business_profile_key=profile.profile_key,
            compilation_rows=(),
        )
        plan = _build_legacy_execution_plan(
            rubric_snapshot=rubric_snapshot,
            profile=profile,
            registry=registry,
            compatibility_nodes=legacy_adapter.adapt_compatibility_nodes(
                criteria=raw_criteria,
                rubric_source_kind="legacy_unversioned",
            ),
        )
        workflow_profile = "template_driven"
    request = {
        "schema_version": "scoring-request@2",
        "submission": paper_snapshots.submission.to_mapping(),
        "document": paper_snapshots.document.to_mapping(),
        "plan": plan.to_mapping(),
        "runtime_identity": profile.build_runtime_identity(scorer),
        "rescore_generation": rescore_generation,
    }
    request["idempotency_key"] = canonical_sha256(
        _request_idempotency_projection(request)
    )
    return (
        ScoringRequest.from_mapping(request),
        registry,
        profile,
        rubric,
        paper,
        workflow_profile,
    )


def _score_paper_core(
    db: Session,
    paper_id: str,
    scorer,
    *,
    rescore_generation: int = 0,
):
    (
        request,
        registry,
        profile,
        rubric,
        paper,
        workflow_profile,
    ) = _core_request_context(
        db,
        paper_id,
        scorer,
        rescore_generation=rescore_generation,
    )
    outcome = score_submission_observed(
        request=request,
        checker_registry=registry,
        llm_runtime=profile.build_llm_runtime(scorer),
        profile=profile,
        organization_id=getattr(paper, "organization_id", None),
        score_fn=score_submission,
    )
    snapshot_store = LocalDocumentSnapshotStore()
    document_snapshot_ref = snapshot_store.put(request.document)
    parsed = read_json(paper.parsed_text_path)
    coherence_findings = list(parsed.get("coherence_findings", []) or [])
    coherence_findings.extend(analyze_semantic_coherence(parsed, scorer))
    format_findings = _compute_format_findings(paper, rubric)
    return CoreRunPersistence(
        db, document_snapshot_store=snapshot_store
    ).persist(
        request=request,
        outcome=outcome,
        paper_id=paper.id,
        rubric_id=rubric.id,
        criterion_id_by_code={
            criterion.code: criterion.id for criterion in rubric.criteria
        },
        workflow_profile=workflow_profile,
        document_snapshot_ref=document_snapshot_ref,
        coherence_findings=coherence_findings,
        format_findings=format_findings,
        ai_connection_snapshot=getattr(scorer, "_ai_connection_snapshot", None),
    )


def _comparison_diff(legacy_run, candidate_outcome):
    candidate = candidate_outcome.to_mapping()
    return {
        "legacy": {
            "total": (
                None
                if legacy_run.final_total_score is None
                else str(legacy_run.final_total_score)
            ),
            "grade": legacy_run.grade,
            "review_required": bool(legacy_run.need_manual_review),
        },
        "candidate": {
            "total": candidate["final_total"],
            "grade": candidate["grade"],
            "status": candidate["status"],
        },
    }


def _record_core_comparison(*, db, paper_id, scorer, legacy_run):
    (
        request,
        registry,
        profile,
        _rubric,
        _paper,
        _workflow_profile,
    ) = _core_request_context(
        db, paper_id, scorer
    )
    outcome = score_submission_observed(
        request=request,
        checker_registry=registry,
        llm_runtime=profile.build_llm_runtime(scorer),
        profile=profile,
        organization_id=getattr(_paper, "organization_id", None),
        score_fn=score_submission,
    )
    outcome_mapping = outcome.to_mapping()
    get_comparison_artifact_sink().record(
        artifact={
            "schema_version": "scoring-comparison-artifact@1",
            "authority": "non_authoritative",
            "legacy_run_id": legacy_run.id,
            "candidate_run_id": None,
            "candidate_identity": deepcopy(outcome_mapping["request_identity"]),
            "candidate_outcome": outcome_mapping,
            "diff": _comparison_diff(legacy_run, outcome),
        }
    )


def collect_scoring_inputs(db: Session, paper) -> ScoringInputs:
    """只读：加载解析结果，按评分项路由预取证据候选/校准锚点，并把评分项快照成纯对象。"""
    rubric = paper.batch.rubric
    authoritative, policy, rubric_snapshot = _prepare_runtime_authority(db, rubric)
    snapshot_criteria = {
        str(_contract_field(item, "code")): item
        for item in (_contract_field(rubric_snapshot, "criteria", default=()) or ())
    } if rubric_snapshot is not None else {}
    parsed = read_json(paper.parsed_text_path)
    plans = []
    for criterion in rubric.criteria:
        route = _route_for(criterion)
        snapshot = _snapshot_criterion(
            criterion,
            authoritative=authoritative,
            policy=policy,
            rubric_snapshot=rubric_snapshot,
            authority_criterion=snapshot_criteria.get(str(criterion.code)),
        )
        plan = CriterionPlan(criterion=snapshot, route=route)
        if route == "chunks":
            plan.candidates = retrieve_for_criterion(db, paper.id, criterion, top_k=settings.SCORING_CHUNK_EVAL_TOP_K)
            plan.anchors = get_anchors(db, rubric.id, criterion.code)  # L2 校准锚点（脱敏范文）
        elif route == "hybrid":
            for index, sub in enumerate(criterion.sub_checks, start=1):
                sub_criterion = _make_sub_criterion(snapshot, sub, index)
                candidates = (
                    []
                    if sub_criterion.criterion_type == "deterministic"
                    else retrieve_for_criterion(db, paper.id, sub_criterion, top_k=settings.SCORING_CHUNK_EVAL_TOP_K)
                )
                plan.sub_plans.append((sub_criterion, candidates))
        plans.append(plan)
    return ScoringInputs(
        paper_id=paper.id,
        paper_title=paper.title,
        parse_quality=paper.parse_quality,
        parsed=parsed,
        structure_checks=parsed.get("structure_checks", []),
        rubric_id=rubric.id,
        rubric_total_score=rubric.total_score,
        rubric_version=rubric.version,
        base_coherence=list(parsed.get("coherence_findings", []) or []),
        format_findings=_compute_format_findings(paper, rubric),
        criteria=plans,
        authoritative=authoritative,
        policy_snapshot=policy,
        rubric_snapshot=rubric_snapshot,
    )


def collect_inputs_from_parsed(
    parsed_obj,
    criteria,
    rubric_id="stateless",
    rubric_total_score=100,
    rubric_version="stateless",
    top_k=None,
    anchors_by_code=None,
    paper_path=None,
    format_spec=None,
):
    """DB-less：从解析对象 + 评分项定义（鸭子类型，含 ImportedCriterion）直接组装 ScoringInputs。

    内存分块 + 内存检索，零 DB；compute_scoring 随后纯算即可。供 `pgs score --no-db` 复用同一内核。
    """
    if top_k is None:
        top_k = settings.SCORING_CHUNK_EVAL_TOP_K
    _validate_authoritative_criteria(criteria)
    parsed = parsed_obj.to_dict()
    chunks = build_chunks(parsed_obj, "stateless")
    for index, chunk in enumerate(chunks):
        chunk.id = "c%d" % index  # 内存 chunk 未落库 → 赋合成 id 供 evidence 引用/校验
    anchors_by_code = anchors_by_code or {}
    weight_validation = validate_weight_configuration(criteria, total_score=rubric_total_score)
    policy = build_corrected_thesis_policy(rubric_total_score, weight_validation.mode)
    rubric_snapshot = _stateless_rubric_snapshot(
        criteria, rubric_id, rubric_total_score
    )
    authority_by_code = {
        str(_contract_field(item, "code")): item
        for item in (_contract_field(rubric_snapshot, "criteria", default=()) or ())
    }
    plans = []
    for criterion in criteria:
        route = _route_for(criterion)
        snapshot = _snapshot_criterion(
            criterion,
            authoritative=True,
            policy=policy,
            rubric_snapshot=rubric_snapshot,
            authority_criterion=authority_by_code.get(str(criterion.code)),
        )
        plan = CriterionPlan(criterion=snapshot, route=route)
        if route == "chunks":
            plan.candidates = retrieve_for_criterion_in_chunks(chunks, criterion, top_k=top_k)
            plan.anchors = anchors_by_code.get(getattr(criterion, "code", None), [])
        elif route == "hybrid":
            for sub_index, sub in enumerate(criterion.sub_checks, start=1):
                sub_criterion = _make_sub_criterion(snapshot, sub, sub_index)
                candidates = (
                    []
                    if sub_criterion.criterion_type == "deterministic"
                    else retrieve_for_criterion_in_chunks(chunks, sub_criterion, top_k=top_k)
                )
                plan.sub_plans.append((sub_criterion, candidates))
        plans.append(plan)
    format_findings = _compute_format_findings(
        SimpleNamespace(file_path=paper_path or ""), SimpleNamespace(format_spec=format_spec or {})
    )
    return ScoringInputs(
        paper_id="stateless",
        paper_title=parsed.get("title"),
        parse_quality=parsed.get("parse_quality"),
        parsed=parsed,
        structure_checks=parsed.get("structure_checks", []),
        rubric_id=rubric_id,
        rubric_total_score=rubric_total_score,
        rubric_version=rubric_version,
        base_coherence=list(parsed.get("coherence_findings", []) or []),
        format_findings=format_findings,
        criteria=plans,
        authoritative=True,
        policy_snapshot=policy,
        rubric_snapshot=rubric_snapshot,
    )


def _prepare_runtime_authority(db: Session, rubric):
    """为已发布 legacy rubric 冻结 M1 权威身份；草稿评分仅保留旧兼容语义。"""

    if getattr(rubric, "status", None) != "published":
        return False, None, None

    weight_validation = validate_weight_configuration(
        rubric.criteria,
        total_score=rubric.total_score,
    )
    policy = build_corrected_thesis_policy(
        rubric.total_score,
        weight_validation.mode,
    )
    _validate_authoritative_criteria(rubric.criteria)
    compilations = list(
        db.scalars(
            select(RubricCompilation).where(RubricCompilation.rubric_id == rubric.id)
        ).all()
    )
    if compilations:
        published_ids = {
            compilation.id
            for compilation in compilations
            if compilation.status == "published"
            and compilation.final_version_hash
        }
        formal_version = db.scalar(
            select(RubricVersion)
            .where(
                RubricVersion.rubric_id == rubric.id,
                RubricVersion.compilation_id.in_(published_ids or {""}),
            )
            .limit(1)
        )
        if formal_version is not None:
            raise ValueError(
                "formal RubricVersion requires the AtomicRule executor; "
                "the legacy arbitrary-points path is forbidden"
            )
        raise ValueError(
            "rubric provenance/compilation exists without a legally published version; "
            "legacy fallback is forbidden"
        )

    from backend.app.services.scoring.adapters.legacy_rubric import adapt_legacy_rubric

    snapshot = adapt_legacy_rubric(rubric, compilations=[])
    return True, policy, snapshot


def _validate_authoritative_criteria(criteria):
    """Shared score-start gate for DB and DB-less authoritative entry points."""

    for criterion in criteria:
        if (
            getattr(criterion, "criterion_type", None) == "hybrid"
            and getattr(criterion, "sub_checks", None)
        ):
            raise ValueError(
                "published legacy hybrid requires the versioned Composite executor; "
                "automatic scoring is blocked until the M5 compatibility adapter"
            )
        if getattr(criterion, "scoring_mode", None) == "banded":
            _authorized_numeric_bands(criterion)


def _stateless_rubric_snapshot(criteria, rubric_id, total_score):
    from backend.app.services.scoring.adapters.legacy_rubric import adapt_legacy_rubric

    return adapt_legacy_rubric(
        {
            "name": str(rubric_id),
            "status": "published",
            "total_score": total_score,
            "criteria": list(criteria),
        },
        compilations=[],
    )


def compute_scoring(inputs: ScoringInputs, scorer) -> ScoringResult:
    """纯函数（不碰主库）：篇章语义一致性 + 逐项评分 + run 级汇总。所有 LLM 调用在此。"""
    paper_ref = SimpleNamespace(
        id=inputs.paper_id,
        title=inputs.paper_title,
        profile_key="thesis",
        profile_version="thesis-v1",
        document_snapshot_hash=_stable_hash(inputs.parsed),
    )
    # 先算篇章/格式 findings（设计§8/§9），供"显式启用"的评分项按规则领取并转扣分。
    coherence_findings = inputs.base_coherence + analyze_semantic_coherence(inputs.parsed, scorer)
    all_findings = coherence_findings + inputs.format_findings

    items = []
    usage_totals = _blank_usage()
    for plan in inputs.criteria:
        criterion = plan.criterion
        if plan.route == "findings":
            if inputs.authoritative:
                output = _unsupported_authoritative_output(
                    criterion,
                    "FINDINGS_OBSERVATION_NOT_REPLAYABLE",
                    "篇章/格式 finding 尚无可从 DocumentSnapshot 重放的 M1 observation，自动分已阻断。",
                )
            else:
                output = score_from_findings(
                    criterion, all_findings, criterion.deduction_rules_structured
                )
        elif plan.route == "deterministic":
            output = run_deterministic_checker(criterion, inputs.parsed)
            if inputs.authoritative:
                output = _validate_deterministic_runtime_output(
                    criterion,
                    output,
                    inputs.parsed,
                    paper_ref.document_snapshot_hash,
                )
        elif plan.route == "hybrid":
            output = _compute_hybrid(
                scorer, paper_ref, criterion, plan.sub_plans, inputs.parsed, inputs.structure_checks, inputs.rubric_version
            )
        else:
            output = _score_criterion_by_chunks(
                scorer, paper_ref, criterion, plan.candidates, inputs.structure_checks, inputs.rubric_version, plan.anchors
            )
        _add_usage(usage_totals, output.get("usage"))
        items.append(
            {
                "criterion_id": criterion.id,
                "criterion_code": criterion.code,
                "max_score": criterion.max_score,
                "weight": getattr(criterion, "weight", None),
                "ai_score": output.get("score"),
                "final_score": output.get("score"),
                "evidence_sufficient": output["evidence_sufficient"],
                "reason": output["reason"],
                "deductions": output["deductions"],
                "deduction_items": output.get("deduction_items") or [],
                "evidence": output["evidence"],
                "band_selection": output.get("band_selection"),
                "sub_results": output.get("sub_results"),
                "suggestion": output["suggestion"],
                "confidence": output["confidence"],
                "need_manual_review": output["need_manual_review"],
                "auto_score_status": output.get("auto_score_status")
                or ("calculated" if output.get("score") is not None else "blocked"),
                "raw_model_output": output,
            }
        )

    if inputs.authoritative:
        ai_total, final_total, grade, need_review = _aggregate_policy_totals(
            inputs.policy_snapshot,
            items,
            inputs.parse_quality,
        )
    else:
        ai_total, final_total, grade, need_review = _aggregate_run_totals(
            items, inputs.parse_quality, inputs.rubric_total_score
        )
    return ScoringResult(
        items=items,
        prompt_tokens=usage_totals["prompt_tokens"],
        completion_tokens=usage_totals["completion_tokens"],
        total_tokens=usage_totals["total_tokens"],
        coherence_findings=coherence_findings,
        format_findings=inputs.format_findings,
        ai_total=ai_total,
        final_total=final_total,
        grade=grade,
        need_review=need_review,
    )


def _unsupported_authoritative_output(criterion, issue_code, reason):
    output = {
        "criterion_id": criterion.id,
        "criterion_name": criterion.name,
        "max_score": float(criterion.max_score),
        "score": None,
        "evidence_sufficient": False,
        "reason": reason,
        "deductions": [],
        "deduction_items": [],
        "evidence": [],
        "suggestion": "请补齐可重放 observation 后重新评分。",
        "confidence": 1.0,
        "need_manual_review": True,
        "usage": _blank_usage(),
    }
    return _blocked_score_output(output, issue_code)


def _validate_deterministic_runtime_output(
    criterion, output, parsed, document_snapshot_hash
):
    """Turn a legacy deterministic result into replayable M1 evidence.

    The score remains a raw result produced by trusted code.  Authorization to
    include it in an authoritative total comes from a versioned observation
    that the Core validator replays against independently derived snapshot
    metrics.  An unconfigured structure checker has no observation and blocks.
    """

    checker_kind = str(output.get("checker_kind") or "")
    replayed_output = run_deterministic_checker(criterion, parsed)
    replay_fields = (
        "checker_kind",
        "score",
        "deduction_items",
        "need_manual_review",
    )
    supplied_effect = {name: output.get(name) for name in replay_fields}
    replayed_effect = {name: replayed_output.get(name) for name in replay_fields}
    if _stable_hash(supplied_effect) != _stable_hash(replayed_effect):
        return _blocked_score_output(
            output, "DETERMINISTIC_SCORE_REPLAY_MISMATCH"
        )
    metric = _deterministic_snapshot_metric(checker_kind, parsed)
    if metric is None:
        return _blocked_score_output(output, "DETERMINISTIC_OBSERVATION_MISSING")

    checker_key = "legacy.deterministic.%s" % checker_kind
    checker_version = "m1-v1"
    locator = {
        "kind": "document_metric",
        "path": "metrics.deterministic.%s" % checker_kind,
    }
    observation = {
        "type": "deterministic_observation",
        "checker_key": checker_key,
        "checker_version": checker_version,
        "locator": locator,
        "observation_code": "LEGACY_%s_RESULT" % checker_kind.upper(),
        "measured_value": metric,
        "expected_value": None,
    }
    policy = getattr(criterion, "evidence_policy", None) or {}
    result = validate_evidence(
        evidence={"items": [observation]},
        evidence_units={},
        policy=policy,
        context={
            "decision_mode": "deterministic",
            "document_snapshot_hash": document_snapshot_hash,
            "snapshot_metrics": {"deterministic": {checker_kind: metric}},
            "checker_manifest": {
                checker_key: {"version": checker_version},
            },
        },
    )
    output["evidence_validation"] = _plain_contract(result)
    output["evidence"] = _plain_contract(result.valid_evidence)
    output["evidence_sufficient"] = result.evidence_sufficient
    output["injection_flagged"] = result.injection_flagged
    output["validation_issues"] = _plain_contract(result.issues)
    output["need_manual_review"] = bool(
        output.get("need_manual_review") or result.review_required
    )
    if result.blocks_final_total or not result.score_effect_allowed:
        return _blocked_score_output(output, "DETERMINISTIC_EVIDENCE_INVALID")
    output["auto_score_status"] = "calculated"
    output["final_total_blocked"] = False
    output["review_required"] = bool(output.get("need_manual_review"))
    return _apply_frozen_review_policy(criterion, output)


def _deterministic_snapshot_metric(checker_kind, parsed):
    full_text = str(parsed.get("full_text") or "")
    if checker_kind == "structure":
        checks = [
            {
                "code": str(check.get("code") or ""),
                "name": str(check.get("name") or ""),
                "passed": bool(check.get("passed")),
                "message": str(check.get("message") or ""),
                "location": str(check.get("location") or ""),
            }
            for check in parsed.get("structure_checks", [])
            if check.get("code") in COMPLETENESS_CODES
        ]
        return checks or None
    if checker_kind == "word_count":
        return {
            "character_count": len(full_text),
            "minimum": WORD_COUNT_MIN_DEFAULT,
        }
    if checker_kind == "figure":
        return {
            "reference_numbers": sorted(
                {int(value) for value in FIGURE_REF_RE.findall(full_text)}
            )
        }
    if checker_kind in {"citation", "citation_author_year"}:
        return {
            "reference_count": len(parsed.get("references") or []),
            "reference_heading_detected": "参考文献" in full_text,
            "in_text_reference_numbers": sorted(
                {int(value) for value in INTEXT_NUM_RE.findall(full_text)}
            ),
            "author_year_detected": bool(AUTHOR_YEAR_RE.search(full_text)),
        }
    return None


def persist_scoring(db: Session, paper_id, inputs: ScoringInputs, result: ScoringResult, scorer, started_at) -> ScoringRun:
    """短写：落库 ScoringRun + ScoreItem（findings 已在 compute 被启用项消费/标记），更新论文状态。"""
    paper = db.get(Paper, paper_id)
    run = ScoringRun(
        paper_id=paper_id,
        owner_id=getattr(paper, "owner_id", None),  # 继承论文归属
        organization_id=getattr(paper, "organization_id", None),
        rubric_id=inputs.rubric_id,
        model_provider=scorer.provider,
        model_name=scorer.model_name,
        model_version=scorer.model_version,
        ai_connection_id=(getattr(scorer, "_ai_connection_snapshot", {}) or {}).get("ai_connection_id"),
        ai_connection_key_version=(getattr(scorer, "_ai_connection_snapshot", {}) or {}).get("key_version"),
        ai_connection_snapshot=getattr(scorer, "_ai_connection_snapshot", None),
        status="scored",
        started_at=started_at,
        finished_at=_utcnow(),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        coherence_findings=result.coherence_findings,
        format_findings=result.format_findings,
        policy_snapshot=(
            _plain_contract(inputs.policy_snapshot) if inputs.authoritative else None
        ),
        policy_hash=(
            _contract_field(inputs.policy_snapshot, "policy_hash")
            if inputs.authoritative
            else None
        ),
        policy_schema_version=(
            _contract_field(inputs.policy_snapshot, "schema_version")
            if inputs.authoritative
            else None
        ),
        ai_total_score=result.ai_total,
        final_total_score=result.final_total,
        grade=result.grade,
        need_manual_review=result.need_review,
    )
    for data in result.items:
        run.items.append(
            ScoreItem(
                criterion_id=data["criterion_id"],
                max_score=data["max_score"],
                ai_score=data["ai_score"],
                final_score=data["final_score"],
                evidence_sufficient=data["evidence_sufficient"],
                reason=data["reason"],
                deductions=_plain_contract(data["deductions"]),
                deduction_items=_plain_contract(data["deduction_items"]),
                evidence=_plain_contract(data["evidence"]),
                band_selection=_plain_contract(data["band_selection"]),
                sub_results=_plain_contract(data["sub_results"]),
                suggestion=data["suggestion"],
                confidence=data["confidence"],
                need_manual_review=data["need_manual_review"],
                raw_model_output=_plain_contract(data["raw_model_output"]),
                aggregation=(data.get("aggregation") if inputs.authoritative else None),
                aggregation_schema_version=(
                    AGGREGATION_SCHEMA_VERSION if inputs.authoritative else None
                ),
                auto_score_status=(
                    data.get("auto_score_status") if inputs.authoritative else None
                ),
            )
        )
    paper.status = "pending_review" if result.need_review else "scored"
    db.add(run)
    db.flush()
    record_usage_ledger(db, run)
    db.commit()
    db.refresh(run)
    return run


def _route_for(criterion):
    """按 criterion_type + findings 启用决定路由（设计§6.2）。"""
    if is_findings_enabled(criterion):
        return "findings"
    criterion_type = getattr(criterion, "criterion_type", "llm_judgment")
    if criterion_type == "deterministic":
        return "deterministic"
    if criterion_type == "hybrid" and getattr(criterion, "sub_checks", None):
        return "hybrid"
    return "chunks"


def _snapshot_criterion(
    criterion,
    *,
    authoritative=False,
    policy=None,
    rubric_snapshot=None,
    authority_criterion=None,
):
    """把 ORM 评分项快照成纯 SimpleNamespace，使 compute 阶段在事务释放后仍可安全读取（鸭子类型，复用现有打分器）。"""
    return SimpleNamespace(
        id=getattr(criterion, "id", None) or getattr(criterion, "code", None),
        code=criterion.code,
        name=criterion.name,
        max_score=criterion.max_score,
        weight=getattr(criterion, "weight", None),
        description=getattr(criterion, "description", None),
        evidence_hints=list(getattr(criterion, "evidence_hints", None) or []),
        deduction_rules=list(getattr(criterion, "deduction_rules", None) or []),
        deduction_rules_structured=list(getattr(criterion, "deduction_rules_structured", None) or []),
        criterion_type=getattr(criterion, "criterion_type", "llm_judgment"),
        scoring_mode=getattr(criterion, "scoring_mode", "llm_direct"),
        applies_to=getattr(criterion, "applies_to", "global"),
        rubric_levels=list(getattr(criterion, "rubric_levels", None) or []),
        sub_checks=list(getattr(criterion, "sub_checks", None) or []),
        dimension=getattr(criterion, "dimension", None),
        authorized_rules=list(
            _contract_field(authority_criterion, "authorized_rules", default=()) or ()
        ),
        rubric_source_kind=_contract_field(
            rubric_snapshot, "rubric_source_kind", default=None
        ),
        rubric_snapshot=rubric_snapshot,
        frozen_policy=policy,
        evidence_policy=_runtime_evidence_policy(policy),
        secure_execution=bool(authoritative),
    )


def _compute_hybrid(scorer, paper_ref, criterion, sub_plans, parsed, structure_checks, rubric_version):
    """混合制（纯，设计§2/§6.3）：sub_plans 已含预取候选；确定性子项走 checker，语义子项走模型，再汇总。"""
    sub_results = []
    usage = _blank_usage()
    for sub_criterion, candidates in sub_plans:
        if sub_criterion.criterion_type == "deterministic":
            sub_output = run_deterministic_checker(sub_criterion, parsed)
        else:
            sub_output = _score_criterion_by_chunks(scorer, paper_ref, sub_criterion, candidates, structure_checks, rubric_version)
        _add_usage(usage, sub_output.get("usage"))
        sub_results.append(sub_output)
    return _aggregate_hybrid(criterion, sub_results, usage)


def _aggregate_run_totals(item_datas, parse_quality, total_score):
    """从 item 数据（dict）复算 run 级汇总，与 _recalculate_run 同口径（复用 rules）。"""
    items = [
        SimpleNamespace(
            id=None,
            ai_score=data["ai_score"],
            final_score=data["final_score"],
            max_score=data["max_score"],
            evidence_sufficient=data["evidence_sufficient"],
            need_manual_review=data["need_manual_review"],
            confidence=data["confidence"],
        )
        for data in item_datas
    ]
    final_total = calculate_total_score(items, total_score=total_score)
    ai_total = calculate_total_score(_AiScoreProxyList(items), total_score=total_score)
    grade = match_grade(final_total)
    need_review = need_manual_review(final_total, items, parse_quality=parse_quality)
    return ai_total, final_total, grade, need_review


def _aggregate_policy_totals(policy_snapshot, item_datas, parse_quality):
    """M1 权威汇总：AI/人工分均通过同一冻结 policy，且逐项保存 contribution。"""

    policy = _compiled_policy(policy_snapshot)
    ai_rows = [_policy_item_row(data, use_final=False) for data in item_datas]
    final_rows = [_policy_item_row(data, use_final=True) for data in item_datas]
    ai_result = aggregate_scores(policy, ai_rows)
    final_result = aggregate_scores(policy, final_rows)
    for data, aggregate_item in zip(item_datas, final_result.items, strict=True):
        data["aggregation"] = _aggregation_projection(aggregate_item)
    item_review = any(bool(data.get("need_manual_review")) for data in item_datas)
    parse_review = (
        parse_quality is not None
        and Decimal(str(parse_quality)) < policy.review.parse_quality_below
    )
    return (
        ai_result.rounded_total,
        final_result.rounded_total,
        final_result.grade,
        bool(final_result.need_manual_review or item_review or parse_review),
    )


def score_batch(db: Session, batch_id: str, rescore: bool = False):
    batch = db.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(selectinload(GradingBatch.papers).selectinload(Paper.scoring_runs))
    )
    if batch is None:
        raise ValueError("batch not found")

    papers = list(batch.papers)
    result = {
        "batch_id": batch.id,
        "total_papers": len(papers),
        "scored_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "run_ids": [],
        "errors": [],
    }
    batch.status = "scoring"
    db.commit()

    scorer = _scorer_for_batch(db, batch)  # 整批复用一个 scorer（连接池跨论文复用），结束时统一关闭
    try:
        for paper in papers:
            if paper.status == "failed" or not paper.parsed_text_path:
                result["failed_count"] += 1
                result["errors"].append(
                    {
                        "paper_id": paper.id,
                        "file_name": paper.file_name,
                        "error": paper.error_message or "paper is not parsed",
                    }
                )
                continue
            if paper.scoring_runs and not rescore:
                result["skipped_count"] += 1
                continue
            try:
                if rescore and paper.scoring_runs:
                    previous = max(
                        paper.scoring_runs,
                        key=lambda item: (item.created_at, item.id),
                    )
                    run = retry_score_paper(db, previous.id, scorer=scorer)
                else:
                    run = score_paper(db, paper.id, scorer=scorer)
            except (ValueError, LLMScoringError) as exc:
                result["failed_count"] += 1
                result["errors"].append({"paper_id": paper.id, "file_name": paper.file_name, "error": str(exc)})
                continue
            result["scored_count"] += 1
            result["run_ids"].append(run.id)
    finally:
        _close_scorer(scorer)

    batch = db.get(GradingBatch, batch_id)
    if result["failed_count"]:
        batch.status = "scored_with_errors"
    elif result["scored_count"] or result["skipped_count"]:
        batch.status = "scored"
    else:
        batch.status = "draft"
    db.commit()
    return result


def update_score_item(db: Session, item_id: str, final_score: float, reason: str, reviewer_id: str):
    item = db.scalar(
        select(ScoreItem)
        .where(ScoreItem.id == item_id)
        .options(
            selectinload(ScoreItem.scoring_run).selectinload(ScoringRun.paper),
            selectinload(ScoreItem.scoring_run).selectinload(ScoringRun.rubric),
            selectinload(ScoreItem.scoring_run).selectinload(ScoringRun.items),
        )
    )
    if item is None:
        raise ValueError("score item not found")
    if final_score < 0 or final_score > float(item.max_score):
        raise ValueError("final_score out of range")
    _require_recalculable_run(item.scoring_run)
    ThesisProfile().assert_ordinary_item_override_allowed(item=item)

    before = item.final_score if item.final_score is not None else item.ai_score
    item.final_score = final_score
    item.need_manual_review = False
    item.scoring_run.status = "reviewing"
    db.add(
        ReviewLog(
            scoring_run_id=item.scoring_run_id,
            score_item_id=item.id,
            reviewer_id=reviewer_id,
            before_score=before,
            after_score=final_score,
            reason=reason,
            policy_hash=item.scoring_run.policy_hash,
            resolution_type=(
                "ordinary_override" if item.scoring_run.policy_hash else None
            ),
        )
    )
    db.flush()
    _recalculate_run(
        item.scoring_run,
        item.scoring_run.items,
        item.scoring_run.paper.parse_quality,
        item.scoring_run.rubric.total_score,
    )
    db.commit()
    db.refresh(item)
    return item


def submit_review(db: Session, run_id: str, reason: str, reviewer_id: str):
    run = db.scalar(
        select(ScoringRun)
        .where(ScoringRun.id == run_id)
        .options(selectinload(ScoringRun.items), selectinload(ScoringRun.paper), selectinload(ScoringRun.rubric))
    )
    if run is None:
        raise ValueError("scoring run not found")
    _require_recalculable_run(run)
    _recalculate_run(run, run.items, run.paper.parse_quality, run.rubric.total_score)
    ThesisProfile().assert_review_submission_allowed(run=run)
    run.status = "reviewed"
    run.need_manual_review = False
    run.paper.status = "reviewed"
    db.add(
        ReviewLog(
            scoring_run_id=run.id,
            score_item_id=None,
            reviewer_id=reviewer_id,
            before_score=run.ai_total_score,
            after_score=run.final_total_score,
            reason=reason,
            policy_hash=run.policy_hash,
            resolution_type="ordinary_override" if run.policy_hash else None,
        )
    )
    db.commit()
    db.refresh(run)
    return run


def _score_criterion_by_chunks(scorer, paper, criterion, candidates, structure_checks, rubric_version, anchors=None):
    mode = getattr(criterion, "scoring_mode", "llm_direct")
    secure_execution = bool(
        getattr(criterion, "secure_execution", False)
        or getattr(criterion, "evidence_policy", None)
    )
    # Cloud adapters always speak PromptEnvelope/evidence_unit_id, including
    # non-authoritative draft previews. Generate the identity once and bridge a
    # non-authoritative chunk reference only for the legacy validator below.
    candidates = _candidates_with_evidence_unit_ids(candidates)
    # 扣分制/分档制必须对整段一次定性：逐块打分再聚合会把各块扣分累加，重复计扣（且分档无意义）。
    # llm_direct：默认逐块判（top_k 调用）；opt-in 整体判一次（多块一次喂入）——大幅减少调用数+整体上下文。
    single_call = mode in ("deductive", "banded") or settings.SCORING_LLM_DIRECT_SINGLE_CALL

    if not candidates:
        raw_output = _score_with_runtime_fallback(scorer, paper, criterion, [], structure_checks, rubric_version, anchors)
        raw_evidence = _plain_contract(raw_output.get("evidence") or [])
        _bridge_evidence_for_legacy_validator(raw_output, [])
        output = validate_score_output(raw_output, criterion, [])
        output["chunk_evaluation_mode"] = "single-empty-evidence"
        output["chunk_scores"] = []
        chunk_outputs = []
        if secure_execution:
            evidence_result, evidence_projection = _validate_runtime_evidence(
                paper, criterion, raw_evidence, [], mode
            )
            _bind_evidence_result(output, evidence_result, evidence_projection)
    elif single_call:
        raw_output = _score_with_runtime_fallback(scorer, paper, criterion, candidates, structure_checks, rubric_version, anchors)
        raw_evidence = _plain_contract(raw_output.get("evidence") or [])
        _bridge_evidence_for_legacy_validator(raw_output, candidates)
        legacy_candidates = _legacy_validation_candidates(candidates)
        output = validate_score_output(raw_output, criterion, legacy_candidates)
        output["chunk_evaluation_mode"] = "single-combined"
        output["chunk_scores"] = []
        chunk_outputs = [output]
        if secure_execution:
            evidence_result, evidence_projection = _validate_runtime_evidence(
                paper, criterion, raw_evidence, candidates, mode
            )
            _bind_evidence_result(output, evidence_result, evidence_projection)
    else:
        chunk_outputs = []
        for index, candidate in enumerate(candidates, start=1):
            raw_output = _score_with_runtime_fallback(scorer, paper, criterion, [candidate], structure_checks, rubric_version, anchors)
            raw_evidence = _plain_contract(raw_output.get("evidence") or [])
            _bridge_evidence_for_legacy_validator(raw_output, [candidate])
            chunk_output = validate_score_output(
                raw_output,
                criterion,
                _legacy_validation_candidates([candidate]),
            )
            if secure_execution:
                evidence_result, evidence_projection = _validate_runtime_evidence(
                    paper, criterion, raw_evidence, [candidate], mode
                )
                _bind_evidence_result(
                    chunk_output, evidence_result, evidence_projection
                )
            chunk_output["chunk_index"] = index
            chunk_output["chunk_id"] = candidate.get("chunk_id")
            chunk_output["chunk_location"] = candidate.get("location") or candidate.get("section_title") or ""
            chunk_outputs.append(chunk_output)
        output = _aggregate_chunk_outputs(criterion, chunk_outputs)

    # 计分模式分流（设计§6.3 / N6）：deductive 代码算分跳过封顶；banded 吸附/采用模型选档；其余走证据门槛。
    if secure_execution:
        return _apply_secure_mode(criterion, output, mode)
    if mode == "deductive":
        return _apply_deductive(criterion, output)
    if mode == "banded":
        return _apply_banded(criterion, output)
    return _apply_evidence_gate(output, criterion, chunk_outputs)


def _apply_secure_mode(criterion, output, mode):
    evidence_result = output.pop("_validated_evidence_result", None)
    if evidence_result is None:
        return _blocked_score_output(output, "EVIDENCE_VALIDATION_MISSING")
    if output.get("_validated_chunk_effect_count") == 0:
        return _blocked_score_output(output, "NO_VALIDATED_CHUNK_SCORE_EFFECT")
    if evidence_result.blocks_final_total:
        return _blocked_score_output(output, "REQUIRED_EVIDENCE_INVALID")
    snapshot = getattr(criterion, "rubric_snapshot", None)
    if mode == "deductive" and snapshot is not None:
        from backend.app.services.scoring.adapters.legacy_rubric import apply_legacy_deductions

        result = apply_legacy_deductions(
            snapshot=snapshot,
            criterion_code=criterion.code,
            model_effects=output.get("deduction_items") or [],
            evidence_result=evidence_result,
            policy=_legacy_execution_policy(criterion),
        )
        output = _bind_legacy_score_result(output, result, criterion, mode)
        return _apply_frozen_review_policy(criterion, output)
    if mode == "banded":
        bands = _numeric_bands(criterion)
        if not bands:
            return _blocked_score_output(output, "AUTHORIZED_BANDS_MISSING")
        model_band = output.get("band_selection") or {}
        if _match_band(model_band.get("level"), bands) is None:
            return _blocked_score_output(output, "BAND_SELECTION_UNAUTHORIZED")
        selected_quote = _compact_evidence_text(model_band.get("evidence_quote"))
        validated_quotes = {
            _compact_evidence_text(item.get("quote"))
            for item in output.get("evidence") or []
            if _compact_evidence_text(item.get("quote"))
        }
        if not selected_quote:
            return _blocked_score_output(output, "BAND_EVIDENCE_QUOTE_REQUIRED")
        if selected_quote not in validated_quotes:
            return _blocked_score_output(output, "BAND_EVIDENCE_NOT_VALIDATED")
        output = _apply_banded(criterion, output)
    elif mode == "llm_direct" and snapshot is not None:
        from backend.app.services.scoring.adapters.legacy_rubric import apply_legacy_direct_compat

        result = apply_legacy_direct_compat(
            snapshot=snapshot,
            criterion_code=criterion.code,
            model_score=output.get("score"),
            evidence_result=evidence_result,
            policy=_legacy_execution_policy(criterion),
        )
        output = _bind_legacy_score_result(output, result, criterion, mode)
        output["evidence_gate_applied"] = True
        return _apply_frozen_review_policy(criterion, output)
    else:
        output = _apply_evidence_gate(output, criterion, [])
    output["auto_score_status"] = "calculated"
    output["final_total_blocked"] = False
    output["review_required"] = bool(output.get("need_manual_review"))
    return _apply_frozen_review_policy(criterion, output)


def _apply_frozen_review_policy(criterion, output):
    """Apply the run-frozen confidence threshold, never deployment settings."""

    policy = getattr(criterion, "frozen_policy", None)
    review = _contract_field(policy, "review", default={}) or {}
    threshold = _contract_field(review, "confidence_below", default=None)
    confidence = output.get("confidence")
    if threshold is not None and confidence is not None:
        if Decimal(str(confidence)) < Decimal(str(threshold)):
            output["need_manual_review"] = True
            output["review_required"] = True
    return output


def _compact_evidence_text(value):
    return " ".join(str(value or "").split())


def _bind_legacy_score_result(output, result, criterion, mode):
    prior_review = bool(
        output.get("review_required") or output.get("need_manual_review")
    )
    prior_issues = list(output.get("validation_issues") or [])
    score = result.auto_score
    output["score"] = None if score is None else float(score)
    output["auto_score_status"] = result.auto_score_status
    output["final_total_blocked"] = result.final_total_blocked
    output["review_required"] = bool(prior_review or result.need_manual_review)
    output["need_manual_review"] = bool(
        prior_review or result.need_manual_review
    )
    output["validation_issues"] = _merge_validation_issues(
        prior_issues,
        _plain_contract(result.validation_issues),
    )
    if mode == "deductive":
        output["deduction_items"] = _plain_contract(result.applied_effects)
        output["deductions"] = [
            "%s（-%s分）" % (item.get("reason") or item["rule_ref"], item["points"])
            for item in output["deduction_items"]
        ]
        if score is not None:
            output["reason"] = "%s 按冻结授权扣分规则核算，得 %.2f/%.2f。" % (
                criterion.name,
                float(score),
                float(criterion.max_score),
            )
    output["scoring_mode"] = mode
    return output


def _blocked_score_output(output, issue_code):
    output["score"] = None
    output["evidence_sufficient"] = False
    output["auto_score_status"] = "invalid"
    output["final_total_blocked"] = True
    output["review_required"] = True
    output["need_manual_review"] = True
    output["validation_issues"] = list(output.get("validation_issues") or []) + [
        {"code": issue_code}
    ]
    return output


def _apply_deductive(criterion, output):
    """扣分制：awarded = clamp(max - Σpoints, 0, max)，不取模型自报 score。"""
    max_score = float(criterion.max_score)
    total_deduction = 0.0
    for item in output.get("deduction_items") or []:
        points = item.get("points")
        if points is not None:
            total_deduction += float(points)
    awarded = max(0.0, min(max_score - total_deduction, max_score))
    output["score_before_deductive"] = output.get("score")
    output["score"] = round(awarded, 2)
    output["scoring_mode"] = "deductive"
    if total_deduction > max_score:
        output["need_manual_review"] = True
        output["deductions"] = list(output.get("deductions") or []) + [
            "扣分合计超过满分，已夹取到 0 并标记人工复核。"
        ]
    output["reason"] = "%s 按扣分制核算：满分 %.2f，扣分合计 %.2f，得 %.2f/%.2f。" % (
        criterion.name,
        max_score,
        total_deduction,
        awarded,
        max_score,
    )
    return output


def _apply_banded(criterion, output):
    """分档制（设计§6.3）：优先采用模型在 band_selection 选的档位（匹配 rubric_levels）；
    模型未给或不匹配时，按模型给分就近吸附到最接近的档位。记录带证据的 BandSelection。"""
    bands = _numeric_bands(criterion)
    if not bands:
        # 无有效档位 → 退回证据门槛，避免分档项无法计分。
        return _apply_evidence_gate(output, criterion, [])
    max_score = float(criterion.max_score)
    model_score = float(output.get("score") or 0)
    model_band = output.get("band_selection") or {}
    chosen = _match_band(model_band.get("level"), bands)
    if chosen is not None:
        basis = "model-band"
    else:
        chosen = min(bands, key=lambda band: (abs(band["points"] - model_score), -band["points"]))
        basis = "score-snap"
    first_evidence = (output.get("evidence") or [{}])[0] if output.get("evidence") else {}
    output["score_before_banded"] = model_score
    output["score"] = round(min(float(chosen["points"]), max_score), 2)
    output["band_selection"] = {
        "level": chosen.get("label"),
        "awarded": output["score"],
        "rationale": model_band.get("rationale") or output.get("reason") or "",
        "rule_ref": getattr(criterion, "code", None),
        "evidence_location": model_band.get("evidence_location") or first_evidence.get("location", ""),
        "evidence_quote": model_band.get("evidence_quote") or first_evidence.get("quote", ""),
    }
    output["band_selection_basis"] = basis
    output["scoring_mode"] = "banded"
    output["reason"] = "%s 按分档制核算：选档「%s」=%.2f/%.2f。" % (
        criterion.name,
        chosen.get("label"),
        output["score"],
        max_score,
    )
    if not output.get("evidence"):
        output["need_manual_review"] = True
    return output


def _match_band(level, bands):
    if not level:
        return None
    level = str(level).strip()
    for band in bands:
        label = str(band.get("label") or "").strip()
        if label and label == level:
            return band
    return None


def _numeric_bands(criterion):
    try:
        return _authorized_numeric_bands(criterion)
    except ValueError:
        return []


def _authorized_numeric_bands(criterion):
    raw_bands = list(getattr(criterion, "rubric_levels", None) or [])
    if not raw_bands:
        raise ValueError(
            "published banded criterion requires at least one authorized numeric rubric level"
        )
    maximum = Decimal(str(getattr(criterion, "max_score")))
    bands = []
    labels = set()
    for index, band in enumerate(raw_bands):
        if not isinstance(band, Mapping):
            raise ValueError("rubric level %d must be an object" % index)
        label = str(band.get("label") or "").strip()
        if not label or label in labels:
            raise ValueError("rubric level labels must be non-empty and unique")
        labels.add(label)
        if isinstance(band.get("points"), bool) or band.get("points") is None:
            raise ValueError("rubric level points must be a finite number")
        try:
            points = Decimal(str(band.get("points")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("rubric level points must be a finite number") from exc
        if not points.is_finite() or points < 0 or points > maximum:
            raise ValueError("rubric level points must be within 0..max_score")
        bands.append({"label": label, "points": float(points)})
    return bands


def _make_sub_criterion(parent, sub, index):
    kind = sub.get("kind") or "llm_judgment"
    return SimpleNamespace(
        id="%s::sub%d" % (getattr(parent, "id", "c"), index),
        code="%s-S%d" % (getattr(parent, "code", "C"), index),
        name=sub.get("name") or sub.get("criteria") or getattr(parent, "name", "子检查"),
        max_score=float(sub.get("max_points") or 0),
        description=sub.get("criteria") or sub.get("description"),
        evidence_hints=list(getattr(parent, "evidence_hints", None) or []),
        deduction_rules=list(getattr(parent, "deduction_rules", None) or []),
        criterion_type=kind,
        scoring_mode="deductive" if kind == "deterministic" else "llm_direct",
        applies_to=getattr(parent, "applies_to", "global"),
        rubric_levels=[],
        sub_checks=[],
        weight=None,
        authorized_rules=[],
        rubric_source_kind=getattr(parent, "rubric_source_kind", None),
        rubric_snapshot=getattr(parent, "rubric_snapshot", None),
        frozen_policy=getattr(parent, "frozen_policy", None),
        evidence_policy=getattr(parent, "evidence_policy", None),
        secure_execution=getattr(parent, "secure_execution", False),
    )


def _aggregate_hybrid(criterion, sub_results, usage):
    max_score = float(criterion.max_score)
    blocked = any(
        item.get("auto_score_status") in {"invalid", "blocked"}
        or item.get("final_total_blocked")
        for item in sub_results
    )
    awarded = (
        None
        if blocked
        else round(
            min(sum(float(item.get("score") or 0) for item in sub_results), max_score),
            2,
        )
    )
    deduction_items = []
    deductions = []
    evidence = []
    for item in sub_results:
        deduction_items.extend(item.get("deduction_items") or [])
        deductions.extend(item.get("deductions") or [])
        evidence.extend(item.get("evidence") or [])
    confidences = [float(item.get("confidence") or 0) for item in sub_results] or [0.0]
    return {
        "criterion_id": criterion.id,
        "criterion_name": criterion.name,
        "max_score": max_score,
        "score": awarded,
        "evidence_sufficient": all(item.get("evidence_sufficient") for item in sub_results) if sub_results else False,
        "reason": (
            "%s 的子检查存在无效/阻断结果，自动分暂不形成。" % criterion.name
            if blocked
            else "%s 按混合制核算：%d 个子检查合计 %.2f/%.2f。"
            % (criterion.name, len(sub_results), awarded, max_score)
        ),
        "deductions": _dedupe_texts(deductions),
        "deduction_items": _dedupe_deduction_items(deduction_items),
        "evidence": _dedupe_evidence(evidence)[:6],
        "suggestion": "混合制：确定性子项由规则核验，语义子项由模型评分。",
        "confidence": round(min(confidences), 3),
        "need_manual_review": blocked
        or any(item.get("need_manual_review") for item in sub_results),
        "auto_score_status": "blocked" if blocked else "calculated",
        "final_total_blocked": blocked,
        "scoring_mode": "hybrid",
        "sub_results": sub_results,
        "usage": usage,
    }


def _score_with_runtime_fallback(scorer, paper, criterion, candidates, structure_checks, rubric_version=None, anchors=None):
    provider = getattr(scorer, "provider", "")
    cache_request = None
    cache_key = None
    envelope = None
    build_envelope = getattr(scorer, "build_prompt_envelope", None)
    score_envelope = getattr(scorer, "score_envelope", None)
    use_envelope = provider != "mock" and callable(build_envelope) and callable(score_envelope)
    # The legacy cache tables predate tenant identity.  BYOK runs therefore do
    # not read or write them until the isolated encrypted-cache migration is
    # active; a miss is safer than any chance of cross-account reuse.
    byok_runtime = bool(getattr(scorer, "_ai_connection_snapshot", None))
    if use_envelope:
        rubric_snapshot = getattr(criterion, "rubric_snapshot", None) or rubric_version
        policy = getattr(criterion, "frozen_policy", None)
        try:
            envelope = build_envelope(
                paper,
                criterion,
                candidates,
                structure_checks,
                rubric_snapshot,
                policy,
                anchors,
            )
        except Exception as exc:
            raise LLMScoringError(
                "PromptEnvelope 构造或身份校验失败：%s" % _short_error(exc)
            ) from exc
    # L0 缓存（设计§7）：仅对真实模型生效；mock 廉价且确定，不缓存。
    if settings.LLM_CACHE_ENABLED and provider != "mock" and not byok_runtime:
        if envelope is not None:
            cache_key = llm_cache.key_of(envelope)
            try:
                entry = llm_cache.get_entry(
                    cache_key,
                    requester_scope="scoring-runtime",
                )
            except Exception:
                entry = None
            cached = entry.response if entry is not None else None
        else:
            cache_request = llm_cache.build_request(
                scorer,
                criterion,
                candidates,
                structure_checks,
                rubric_version,
                anchors,
            )
            cache_key = llm_cache.key_of(cache_request)
            cached = llm_cache.get(cache_key)
        if cached is not None and not isinstance(cached, Mapping):
            cached = None
        if cached is not None:
            cached = dict(cached)
            cached["cache_hit"] = True
            cached["cache_key"] = cache_key
            cached["usage"] = _blank_usage()  # 命中不计新增 token 成本
            return cached

    try:
        _throttle_real_llm_call(scorer)
        output = (
            score_envelope(envelope)
            if envelope is not None
            else scorer.score_criterion(
                paper, criterion, candidates, structure_checks, anchors
            )
        )
    except Exception as exc:
        if not settings.LLM_FALLBACK_TO_MOCK or provider == "mock":
            raise LLMScoringError("真实模型调用失败：%s" % _short_error(exc)) from exc

        fallback = MockLLMScorer()
        output = fallback.score_criterion(paper, criterion, candidates, structure_checks, anchors)
        fallback_reason = _short_error(exc)
        output["provider"] = fallback.provider
        output["fallback_from_provider"] = getattr(scorer, "provider", "unknown")
        output["fallback_from_model"] = getattr(scorer, "model_name", "unknown")
        output["fallback_reason"] = fallback_reason
        output["need_manual_review"] = True
        output["confidence"] = min(float(output.get("confidence", 0)), 0.6)
        output["deductions"] = list(output.get("deductions") or []) + [
            "真实模型调用失败，已自动降级为 Mock 评分：%s" % fallback_reason
        ]
        output["suggestion"] = "%s 请人工重点复核，并检查 LLM API 网络、Base URL、API Key 或地区访问限制。" % (
            output.get("suggestion") or ""
        )
        return output

    # 真实调用成功：写入 L0 账本（完整 request+response），供复用与审计。
    if cache_key is not None:
        output["cache_hit"] = False
        output["cache_key"] = cache_key
        if envelope is not None:
            try:
                llm_cache.put_envelope(
                    envelope=envelope,
                    response=output,
                    policy=llm_cache.CacheRetentionPolicy(
                        retention_seconds=30 * 24 * 60 * 60,
                        store_controlled_original=False,
                        allowed_scopes=("scoring-runtime", "scoring-audit"),
                    ),
                    access_scope="scoring-runtime",
                )
            except Exception:
                # L0 是最佳努力账本；写入失败不能改变已完成的评分结果。
                pass
        else:
            llm_cache.put(
                cache_key,
                cache_request,
                output,
                model=getattr(scorer, "model_name", ""),
            )
    return output


def _throttle_real_llm_call(scorer):
    if getattr(scorer, "provider", "") == "mock":
        return
    delay_seconds = max(0, float(settings.LLM_RATE_LIMIT_SLEEP_SECONDS))
    if delay_seconds <= 0:
        return
    log_llm_throttle_sleep(getattr(scorer, "provider", "unknown"), delay_seconds)
    time.sleep(delay_seconds)


def _aggregate_chunk_outputs(criterion, chunk_outputs):
    max_score = float(criterion.max_score)
    secure_aggregation = any(
        output.get("_validated_evidence_result") is not None
        for output in chunk_outputs
    )

    def effect_allowed(output):
        if not secure_aggregation:
            return True
        evidence_result = output.get("_validated_evidence_result")
        return bool(
            evidence_result is not None
            and evidence_result.score_effect_allowed
            and not evidence_result.blocks_final_total
        )

    authorized_outputs = [output for output in chunk_outputs if effect_allowed(output)]
    weighted_score = 0
    total_weight = 0
    for output in authorized_outputs:
        confidence = float(output.get("confidence") or 0.5)
        weight = (
            confidence
            if secure_aggregation or output.get("evidence_sufficient")
            else confidence * 0.6
        )
        weighted_score += float(output["score"]) * weight
        total_weight += weight

    score = round(weighted_score / total_weight, 2) if total_weight else 0
    confidence_outputs = authorized_outputs or chunk_outputs
    confidence = round(
        sum(float(output.get("confidence") or 0) for output in confidence_outputs)
        / len(confidence_outputs),
        3,
    )
    evidence = []
    deductions = []
    deduction_items = []
    chunk_scores = []
    need_review = False
    evidence_sufficient = False
    for output in chunk_outputs:
        allowed = effect_allowed(output)
        evidence_sufficient = evidence_sufficient or (
            allowed
            if secure_aggregation
            else bool(output.get("evidence_sufficient"))
        )
        need_review = need_review or bool(output.get("need_manual_review"))
        if allowed:
            deductions.extend(output.get("deductions") or [])
            deduction_items.extend(output.get("deduction_items") or [])
            evidence.extend(output.get("evidence") or [])
        chunk_score = {
            "chunk_id": output.get("chunk_id"),
            "location": output.get("chunk_location"),
            "score": output.get("score"),
            "max_score": max_score,
            "confidence": output.get("confidence"),
            "evidence_sufficient": output.get("evidence_sufficient"),
        }
        if secure_aggregation:
            chunk_score["score_effect_allowed"] = allowed
        chunk_scores.append(chunk_score)

    deductions = _dedupe_texts(deductions)
    deduction_items = _dedupe_deduction_items(deduction_items)
    evidence = _dedupe_evidence(evidence)[:6]
    score = max(0, min(score, max_score))
    result = {
        "criterion_id": criterion.id,
        "criterion_name": criterion.name,
        "max_score": max_score,
        "score": score,
        "evidence_sufficient": evidence_sufficient,
        "reason": _aggregate_reason(
            criterion.name,
            score,
            max_score,
            [
                item
                for item in chunk_scores
                if not secure_aggregation or item["score_effect_allowed"]
            ],
        ),
        "deductions": deductions,
        "deduction_items": deduction_items,
        "evidence": evidence,
        "suggestion": "已按分块证据评分汇总；建议人工重点查看低分块、证据不足块和被封顶的评分项。",
        "confidence": confidence,
        "need_manual_review": need_review or confidence < 0.72,
        "chunk_evaluation_mode": "per-evidence-chunk",
        "chunk_scores": chunk_scores,
        "usage": _sum_usage(chunk_outputs),
        "cache_hits": sum(1 for output in chunk_outputs if output.get("cache_hit")),
        "cache_keys": [output.get("cache_key") for output in chunk_outputs if output.get("cache_key")],
    }
    evidence_results = [
        output.get("_validated_evidence_result")
        for output in chunk_outputs
        if output.get("_validated_evidence_result") is not None
    ]
    if evidence_results:
        result["_validated_evidence_result"] = next(
            (
                evidence_result
                for evidence_result in evidence_results
                if evidence_result.score_effect_allowed
                and not evidence_result.blocks_final_total
            ),
            evidence_results[0],
        )
        result["_validated_chunk_effect_count"] = len(authorized_outputs)
        result["_validated_chunk_count"] = len(chunk_outputs)
        result["evidence_validation"] = _plain_contract(
            result["_validated_evidence_result"]
        )
        result["validation_issues"] = [
            issue
            for evidence_result in evidence_results
            for issue in _plain_contract(evidence_result.issues)
        ]
    return result


def _merge_validation_issues(*collections):
    merged = []
    for collection in collections:
        for issue in collection or []:
            plain = _plain_contract(issue)
            if plain not in merged:
                merged.append(plain)
    return merged


def _apply_evidence_gate(output, criterion, chunk_outputs):
    """证据门槛（取代 0.8 常规封顶，设计§10.3）：
    - 证据充分且可信 → 信任模型分，不再硬封顶；
    - 证据不足/未通过原文校验 → 下调到证据门槛上限并标记复核；
    - 低置信度、或接近满分但证据不够强 → 标记复核。"""
    max_score = float(criterion.max_score)
    score = float(output["score"])
    confidence = float(output.get("confidence") or 0)
    evidence_sufficient = bool(output.get("evidence_sufficient"))
    has_evidence = bool(output.get("evidence"))
    output["score_before_gate"] = score

    if not evidence_sufficient or not has_evidence:
        cap_ratio = max(0.0, min(float(settings.SCORING_INSUFFICIENT_EVIDENCE_CAP_RATIO), 1.0))
        cap_score = round(max_score * cap_ratio, 2)
        if score > cap_score:
            output["deductions"] = _dedupe_texts(
                list(output.get("deductions") or [])
                + ["证据不足或未通过原文校验：单项得分按证据门槛 %.0f%% 下调，请人工复核。" % (cap_ratio * 100)]
            )
            output["reason"] = "%s 证据不足，原始 %.2f/%.2f 按证据门槛下调为 %.2f/%.2f。" % (
                output.get("reason") or criterion.name,
                score,
                max_score,
                cap_score,
                max_score,
            )
            score = cap_score
        output["need_manual_review"] = True

    if confidence < float(settings.SCORING_CONFIDENCE_REVIEW_THRESHOLD):
        output["need_manual_review"] = True

    if max_score and score / max_score >= float(settings.SCORING_FULL_SCORE_REVIEW_RATIO):
        if not (evidence_sufficient and confidence >= float(settings.SCORING_HIGH_CONFIDENCE)):
            output["need_manual_review"] = True

    output["score"] = round(max(0.0, min(score, max_score)), 2)
    output["evidence_gate_applied"] = True
    return output


def _aggregate_reason(name, score, max_score, chunk_scores):
    details = "；".join(
        "%s %.2f/%.2f" % (item.get("location") or item.get("chunk_id") or "证据块", float(item.get("score") or 0), max_score)
        for item in chunk_scores[:5]
    )
    return "%s采用分块证据评分并加权汇总，汇总得分 %.2f/%.2f。分块结果：%s。" % (name, score, max_score, details)


def _dedupe_texts(items):
    result = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _dedupe_deduction_items(items):
    result = []
    seen = set()
    for item in items:
        key = (item.get("reason"), item.get("points"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_evidence(items):
    result = []
    seen = set()
    for item in items:
        key = (item.get("chunk_id"), item.get("quote"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _contract_field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default) if value is not None else default


def _plain_contract(value):
    """把冻结 Core 合同投影为 JSON-ready 数据，不改变权威对象本身。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if hasattr(value, "to_mapping"):
        return _plain_contract(value.to_mapping())
    if hasattr(value, "model_dump"):
        return _plain_contract(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _plain_contract(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_contract(item) for item in value]
    if is_dataclass(value):
        return {
            item.name: _plain_contract(getattr(value, item.name))
            for item in fields(value)
        }
    return str(value)


def _hashable_contract(value):
    """canonical_json 禁止 float；运行快照散列前显式转成十进制字符串。"""

    if isinstance(value, float):
        return _decimal_text(Decimal(str(value)))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        return {str(key): _hashable_contract(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hashable_contract(item) for item in value]
    return value


def _stable_hash(value):
    return canonical_sha256(_hashable_contract(_plain_contract(value)))


def _decimal_text(value):
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _runtime_evidence_policy(policy):
    if policy is None:
        return None
    evidence = _contract_field(policy, "evidence", default={}) or {}
    return _plain_contract(evidence)


def _legacy_execution_policy(criterion):
    frozen_policy = getattr(criterion, "frozen_policy", None)
    evidence_policy = getattr(criterion, "evidence_policy", None) or _runtime_evidence_policy(
        frozen_policy
    )
    legacy_policy = _plain_contract(
        _contract_field(frozen_policy, "legacy", default={}) or {}
    )
    return {
        "schema_version": "legacy-execution-policy@1",
        "policy_hash": _contract_field(
            frozen_policy, "policy_hash", default=None
        ),
        "evidence": evidence_policy,
        "evidence_policy_hash": canonical_sha256(evidence_policy),
        "review": _plain_contract(
            _contract_field(frozen_policy, "review", default={}) or {}
        ),
        # Never synthesize compatibility authority at execution time.  These
        # flags came from the persisted/hash-bound ScoringPolicy snapshot.
        "legacy": legacy_policy,
    }


def _evidence_units(candidates):
    units = {}
    by_chunk = {}
    by_unit = {}
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, Mapping):
            continue
        unit_id = _candidate_evidence_unit_id(candidate, index)
        if unit_id in units:
            unit_id = _stable_hash(
                {"duplicate_of": unit_id, "unit_ordinal": index}
            )
        units[unit_id] = {
            "evidence_unit_id": unit_id,
            "text": str(candidate.get("text") or ""),
            "section_id": candidate.get("section_id"),
            "section_path": candidate.get("section_path") or [],
            "section_ordinal": candidate.get("section_ordinal", index),
            "unit_ordinal": candidate.get("unit_ordinal", index),
        }
        if candidate.get("chunk_id") is not None:
            by_chunk[str(candidate.get("chunk_id"))] = unit_id
            by_unit[unit_id] = candidate.get("chunk_id")
        else:
            by_unit[unit_id] = unit_id
    return units, by_chunk, by_unit


def _candidates_with_evidence_unit_ids(candidates):
    result = []
    seen = set()
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, Mapping):
            continue
        unit_id = _candidate_evidence_unit_id(candidate, index)
        if unit_id in seen:
            unit_id = _stable_hash(
                {"duplicate_of": unit_id, "unit_ordinal": index}
            )
        seen.add(unit_id)
        result.append({**candidate, "evidence_unit_id": unit_id})
    return result


def _candidate_evidence_unit_id(candidate, index):
    supplied = candidate.get("evidence_unit_id")
    if (
        isinstance(supplied, str)
        and len(supplied) == 64
        and supplied == supplied.lower()
        and all(character in "0123456789abcdef" for character in supplied)
    ):
        return supplied
    return _stable_hash(
        {
            "unit_ordinal": index,
            "text": str(candidate.get("text") or ""),
            "location": candidate.get("location") or "",
            "section_title": candidate.get("section_title") or "",
        }
    )


def _legacy_validation_candidates(candidates):
    units, _by_chunk, by_unit = _evidence_units(candidates)
    result = []
    for unit_id, unit in units.items():
        source = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("chunk_id") == by_unit[unit_id]
                or candidate.get("evidence_unit_id") == unit_id
            ),
            {},
        )
        result.append({**source, "chunk_id": by_unit[unit_id], "text": unit["text"]})
    return result


def _validate_runtime_evidence(paper, criterion, raw_evidence, candidates, decision_mode):
    units, by_chunk, by_unit = _evidence_units(candidates)
    evidence_items = []
    for item in raw_evidence or []:
        if not isinstance(item, Mapping):
            evidence_items.append(item)
            continue
        evidence_type = item.get("type") or "source_quote"
        projected = dict(item)
        projected["type"] = evidence_type
        if evidence_type == "source_quote":
            unit_id = item.get("evidence_unit_id")
            if not unit_id and item.get("chunk_id") is not None:
                unit_id = by_chunk.get(str(item.get("chunk_id")))
            projected["evidence_unit_id"] = unit_id
            projected.pop("chunk_id", None)
        evidence_items.append(projected)
    expected_ids = list(units)
    document_hash = _contract_field(paper, "document_snapshot_hash", default=None)
    if not isinstance(document_hash, str) or len(document_hash) != 64:
        document_hash = _stable_hash(list(units.values()))
    evidence_policy = getattr(criterion, "evidence_policy", None) or {
        "schema_version": "evidence-policy-v1",
        "requirement": "required",
        "minimum_valid_items": 1,
        "allowed_types": ["source_quote", "deterministic_observation", "scoped_absence"],
        "review_on_optional_invalid": False,
        "absence": {
            "enabled": True,
            "policy_version": "absence-policy-v1",
            "allowed_targets": ["risk_owner"],
            "complete_scope_selectors": ["document://all-units"],
        },
    }
    result = validate_evidence(
        evidence={"items": evidence_items},
        evidence_units=units,
        policy=evidence_policy,
        context={
            "decision_mode": decision_mode,
            "document_snapshot_hash": document_hash,
            "declared_scope": {
                # Retrieval candidates are not an authoritative all-document
                # manifest. Source quotes remain valid; scoped absence fails
                # closed until a complete snapshot manifest is available.
                "selector": "retrieval://candidate-units",
                "expected_evidence_unit_ids": expected_ids,
                "expected_evidence_unit_ids_hash": canonical_sha256(sorted(expected_ids)),
            },
        },
    )
    projection = []
    for item in result.valid_evidence:
        plain = _plain_contract(item)
        unit_id = plain.get("evidence_unit_id")
        location = plain.get("location")
        if isinstance(location, Mapping):
            location = location.get("section_title") or str(location)
        projection.append(
            {
                **plain,
                "location": location or "",
                "chunk_id": by_unit.get(unit_id),
            }
        )
    return result, projection


def _bridge_evidence_for_legacy_validator(output, candidates):
    """旧 validator 仅懂 chunk_id；只在其兼容投影上补 source ref，Core 身份仍是 unit id。"""

    _units, _by_chunk, by_unit = _evidence_units(candidates)
    bridged = []
    for item in output.get("evidence") or []:
        if not isinstance(item, Mapping):
            bridged.append(item)
            continue
        value = dict(item)
        if value.get("chunk_id") is None and value.get("evidence_unit_id"):
            value["chunk_id"] = by_unit.get(value.get("evidence_unit_id"))
        bridged.append(value)
    output["evidence"] = bridged


def _bind_evidence_result(output, result, evidence_projection):
    output["_validated_evidence_result"] = result
    output["evidence_validation"] = _plain_contract(result)
    output["evidence"] = evidence_projection
    output["evidence_sufficient"] = result.evidence_sufficient
    output["injection_flagged"] = result.injection_flagged
    output["validation_issues"] = _plain_contract(result.issues)
    output["need_manual_review"] = bool(
        output.get("need_manual_review") or result.review_required
    )


def _compiled_policy(snapshot):
    total_score = _contract_field(
        _contract_field(snapshot, "aggregation", default={}),
        "total_score",
    )
    return compile_scoring_policy(snapshot, total_score=total_score)


def _policy_item_row(data, *, use_final):
    row = {
        "criterion_code": data["criterion_code"],
        "raw_score": data.get("ai_score"),
        "max_score": data["max_score"],
        "weight": data.get("weight"),
        "auto_score_status": data.get("auto_score_status") or "calculated",
    }
    if use_final:
        row["final_score"] = data.get("final_score")
    return row


def _aggregation_projection(item):
    return {
        "criterion_code": item.criterion_code,
        "raw": None if item.raw_score is None else _decimal_text(item.raw_score),
        "max": _decimal_text(item.max_score),
        "weight": None if item.weight is None else _decimal_text(item.weight),
        "contribution": (
            None if item.contribution is None else _decimal_text(item.contribution)
        ),
    }


def _short_error(exc):
    text = str(exc).strip() or exc.__class__.__name__
    return " ".join(text.split())[:240]


def _recalculate_run(run, items, parse_quality, total_score):
    if run.policy_snapshot is not None:
        policy = _compiled_policy(run.policy_snapshot)
        frozen_weights = _frozen_criterion_weights(run)
        ai_rows = [
            _stored_policy_item(
                item,
                use_final=False,
                frozen_weights=frozen_weights,
            )
            for item in items
        ]
        final_rows = [
            _stored_policy_item(
                item,
                use_final=True,
                frozen_weights=frozen_weights,
            )
            for item in items
        ]
        ai_result = aggregate_scores(policy, ai_rows)
        final_result = aggregate_scores(policy, final_rows)
        for item, aggregate_item in zip(items, final_result.items, strict=True):
            if item.aggregation_schema_version not in {
                "criterion-aggregation@1",
                "criterion-aggregation@2",
            }:
                item.aggregation = _aggregation_projection(aggregate_item)
                item.aggregation_schema_version = AGGREGATION_SCHEMA_VERSION
        run.ai_total_score = ai_result.rounded_total
        run.final_total_score = final_result.rounded_total
        run.grade = final_result.grade
        parse_review = (
            parse_quality is not None
            and Decimal(str(parse_quality)) < policy.review.parse_quality_below
        )
        run.need_manual_review = bool(
            final_result.need_manual_review
            or parse_review
            or any(item.need_manual_review for item in items)
        )
        return
    total = calculate_total_score(items, total_score=total_score)
    run.ai_total_score = calculate_total_score(_AiScoreProxyList(items), total_score=total_score)
    run.final_total_score = total
    run.grade = match_grade(total)
    run.need_manual_review = need_manual_review(total, items, parse_quality=parse_quality)


def _require_recalculable_run(run):
    if run.policy_snapshot is None and getattr(run.rubric, "status", None) == "published":
        raise ValueError(
            "historical run has no frozen policy snapshot and cannot be recalculated; retry to create a new run"
        )
    if run.policy_snapshot is not None:
        policy = _compiled_policy(run.policy_snapshot)
        if policy.policy_hash != run.policy_hash:
            raise ValueError("frozen policy identity does not match scoring run policy_hash")
        if run.policy_schema_version != policy.schema_version:
            raise ValueError(
                "frozen policy schema identity does not match scoring run"
            )
        if policy.usage != "authoritative_new_runs":
            raise ValueError(
                "golden/compare policy cannot be recalculated as an authoritative run"
            )
        for item in getattr(run, "items", ()) or ():
            schema_version = item.aggregation_schema_version
            if schema_version not in {
                AGGREGATION_SCHEMA_VERSION,
                "criterion-aggregation@1",
                "criterion-aggregation@2",
            }:
                raise ValueError(
                    "score item aggregation schema does not match the frozen run"
                )
            if schema_version.startswith("criterion-aggregation@"):
                aggregation = item.aggregation or {}
                if aggregation.get("schema_version") != schema_version:
                    raise ValueError(
                        "Core criterion aggregation identity does not match the score item"
                    )


def _frozen_criterion_weights(run):
    plan = run.execution_plan_snapshot or {}
    weights = {}
    for node in plan.get("nodes", ()) or ():
        criterion = node.get("criterion_snapshot") or {}
        code = criterion.get("criterion_code")
        if not code:
            continue
        weight = criterion.get("weight")
        if code in weights and weights[code] != weight:
            raise ValueError("execution plan contains inconsistent criterion weights")
        weights[code] = weight
    return weights


def _stored_policy_item(item, *, use_final, frozen_weights=None):
    aggregation = item.aggregation or {}
    criterion_code = (
        aggregation.get("criterion_code")
        or item.criterion_code
        or item.criterion_id
    )
    if "weight" in aggregation:
        weight = aggregation.get("weight")
    else:
        weight = (frozen_weights or {}).get(criterion_code)
    row = {
        "criterion_code": criterion_code,
        "raw_score": item.ai_score,
        "max_score": item.max_score,
        "weight": weight,
        "auto_score_status": item.auto_score_status or "calculated",
    }
    if use_final:
        row["final_score"] = item.final_score
    return row


class _AiScoreProxyList:
    def __init__(self, items):
        self.items = items

    def __iter__(self):
        for item in self.items:
            yield _AiScoreProxy(item)


class _AiScoreProxy:
    def __init__(self, item):
        self.id = item.id
        self.ai_score = item.ai_score
        self.final_score = item.ai_score
        self.max_score = item.max_score


def _compute_format_findings(paper, rubric):
    """仅当模板规定了格式（format_spec 有非空值）且被评论文是 docx 时才比对；否则不产出（含 PDF 无法判定）。"""
    expected = getattr(rubric, "format_spec", None) or {}
    if not any(value is not None for key, value in expected.items() if key != "source"):
        return []
    path = paper.file_path or ""
    if not path.lower().endswith(".docx"):
        return []
    try:
        actual = resolve_default_format(materialize(path))
    except Exception:
        return []
    return compare_format(actual, expected)


def _blank_usage():
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _add_usage(totals, usage):
    if not usage:
        return
    for key in totals:
        value = usage.get(key)
        if value:
            totals[key] += int(value)


def _sum_usage(outputs):
    totals = _blank_usage()
    for output in outputs:
        _add_usage(totals, output.get("usage"))
    return totals


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
