from datetime import datetime
from datetime import timezone
from dataclasses import dataclass
from dataclasses import field
from types import SimpleNamespace
import time

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.services.llm.base import LLMScoringError
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.llm.debug_logging import log_llm_throttle_sleep
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.cache import llm_cache
from backend.app.services.calibration import get_anchors
from backend.app.services.checkers import run_deterministic_checker
from backend.app.services.checkers.findings_checker import is_findings_enabled
from backend.app.services.checkers.findings_checker import score_from_findings
from backend.app.services.coherence import analyze_semantic_coherence
from backend.app.services.document_parser.format_check import compare_format
from backend.app.services.document_parser.format_resolver import resolve_default_format
from backend.app.services.document_parser.chunking import build_chunks
from backend.app.services.retrieval.keyword import retrieve_for_criterion
from backend.app.services.retrieval.keyword import retrieve_for_criterion_in_chunks
from backend.app.services.scoring.rules import calculate_total_score
from backend.app.services.scoring.rules import match_grade
from backend.app.services.scoring.rules import need_manual_review
from backend.app.services.scoring.validator import validate_score_output
from backend.app.services.storage.local import read_json


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
    rubric_version: object
    base_coherence: list
    format_findings: list
    criteria: list  # [CriterionPlan]


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


def score_paper(db: Session, paper_id: str, scorer=None):
    """编排：预取(读) → 释放事务 → 纯计算(LLM，无锁) → 落库(短写)。

    把 LLM 调用移出 DB 事务，使 `--workers` 真正并行、避免 sqlite 写锁/快照串行；
    签名与返回（ScoringRun）保持不变，所有调用方（Web/CLI/eval/score_batch）无需改动。
    """
    scorer = scorer or get_llm_scorer()
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

    started_at = _utcnow()
    inputs = collect_scoring_inputs(db, paper)
    # 结束事务：纯计算阶段不持锁/快照（sqlite 并发关键，避免 BUSY_SNAPSHOT）。
    # 用 commit 而非 rollback —— 保留调用方在本会话里既有的未提交改动（如 eval 批量先建的 batch/paper）。
    db.commit()
    result = compute_scoring(inputs, scorer)
    return persist_scoring(db, paper_id, inputs, result, scorer, started_at)


def collect_scoring_inputs(db: Session, paper) -> ScoringInputs:
    """只读：加载解析结果，按评分项路由预取证据候选/校准锚点，并把评分项快照成纯对象。"""
    rubric = paper.batch.rubric
    parsed = read_json(paper.parsed_text_path)
    plans = []
    for criterion in rubric.criteria:
        route = _route_for(criterion)
        snapshot = _snapshot_criterion(criterion)
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
    parsed = parsed_obj.to_dict()
    chunks = build_chunks(parsed_obj, "stateless")
    for index, chunk in enumerate(chunks):
        chunk.id = "c%d" % index  # 内存 chunk 未落库 → 赋合成 id 供 evidence 引用/校验
    anchors_by_code = anchors_by_code or {}
    plans = []
    for criterion in criteria:
        route = _route_for(criterion)
        snapshot = _snapshot_criterion(criterion)
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
    )


def compute_scoring(inputs: ScoringInputs, scorer) -> ScoringResult:
    """纯函数（不碰主库）：篇章语义一致性 + 逐项评分 + run 级汇总。所有 LLM 调用在此。"""
    paper_ref = SimpleNamespace(id=inputs.paper_id, title=inputs.paper_title)
    # 先算篇章/格式 findings（设计§8/§9），供"显式启用"的评分项按规则领取并转扣分。
    coherence_findings = inputs.base_coherence + analyze_semantic_coherence(inputs.parsed, scorer)
    all_findings = coherence_findings + inputs.format_findings

    items = []
    usage_totals = _blank_usage()
    for plan in inputs.criteria:
        criterion = plan.criterion
        if plan.route == "findings":
            output = score_from_findings(criterion, all_findings, criterion.deduction_rules_structured)
        elif plan.route == "deterministic":
            output = run_deterministic_checker(criterion, inputs.parsed)
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
                "max_score": criterion.max_score,
                "ai_score": output["score"],
                "final_score": output["score"],
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
                "raw_model_output": output,
            }
        )

    ai_total, final_total, grade, need_review = _aggregate_run_totals(items, inputs.parse_quality, inputs.rubric_total_score)
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


def persist_scoring(db: Session, paper_id, inputs: ScoringInputs, result: ScoringResult, scorer, started_at) -> ScoringRun:
    """短写：落库 ScoringRun + ScoreItem（findings 已在 compute 被启用项消费/标记），更新论文状态。"""
    paper = db.get(Paper, paper_id)
    run = ScoringRun(
        paper_id=paper_id,
        rubric_id=inputs.rubric_id,
        model_provider=scorer.provider,
        model_name=scorer.model_name,
        model_version=scorer.model_version,
        status="scored",
        started_at=started_at,
        finished_at=_utcnow(),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        coherence_findings=result.coherence_findings,
        format_findings=result.format_findings,
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
                deductions=data["deductions"],
                deduction_items=data["deduction_items"],
                evidence=data["evidence"],
                band_selection=data["band_selection"],
                sub_results=data["sub_results"],
                suggestion=data["suggestion"],
                confidence=data["confidence"],
                need_manual_review=data["need_manual_review"],
                raw_model_output=data["raw_model_output"],
            )
        )
    paper.status = "pending_review" if result.need_review else "scored"
    db.add(run)
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


def _snapshot_criterion(criterion):
    """把 ORM 评分项快照成纯 SimpleNamespace，使 compute 阶段在事务释放后仍可安全读取（鸭子类型，复用现有打分器）。"""
    return SimpleNamespace(
        id=getattr(criterion, "id", None) or getattr(criterion, "code", None),
        code=criterion.code,
        name=criterion.name,
        max_score=criterion.max_score,
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
            run = score_paper(db, paper.id)
        except (ValueError, LLMScoringError) as exc:
            result["failed_count"] += 1
            result["errors"].append({"paper_id": paper.id, "file_name": paper.file_name, "error": str(exc)})
            continue
        result["scored_count"] += 1
        result["run_ids"].append(run.id)

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
    _recalculate_run(run, run.items, run.paper.parse_quality, run.rubric.total_score)
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
        )
    )
    db.commit()
    db.refresh(run)
    return run


def _score_criterion_by_chunks(scorer, paper, criterion, candidates, structure_checks, rubric_version, anchors=None):
    mode = getattr(criterion, "scoring_mode", "llm_direct")
    # 扣分制/分档制必须对整段一次定性：逐块打分再聚合会把各块扣分累加，重复计扣（且分档无意义）。
    single_call = mode in ("deductive", "banded")

    if not candidates:
        raw_output = _score_with_runtime_fallback(scorer, paper, criterion, [], structure_checks, rubric_version, anchors)
        output = validate_score_output(raw_output, criterion, [])
        output["chunk_evaluation_mode"] = "single-empty-evidence"
        output["chunk_scores"] = []
        chunk_outputs = []
    elif single_call:
        raw_output = _score_with_runtime_fallback(scorer, paper, criterion, candidates, structure_checks, rubric_version, anchors)
        output = validate_score_output(raw_output, criterion, candidates)
        output["chunk_evaluation_mode"] = "single-combined"
        output["chunk_scores"] = []
        chunk_outputs = [output]
    else:
        chunk_outputs = []
        for index, candidate in enumerate(candidates, start=1):
            raw_output = _score_with_runtime_fallback(scorer, paper, criterion, [candidate], structure_checks, rubric_version, anchors)
            chunk_output = validate_score_output(raw_output, criterion, [candidate])
            chunk_output["chunk_index"] = index
            chunk_output["chunk_id"] = candidate.get("chunk_id")
            chunk_output["chunk_location"] = candidate.get("location") or candidate.get("section_title") or ""
            chunk_outputs.append(chunk_output)
        output = _aggregate_chunk_outputs(criterion, chunk_outputs)

    # 计分模式分流（设计§6.3 / N6）：deductive 代码算分跳过封顶；banded 吸附/采用模型选档；其余走证据门槛。
    if mode == "deductive":
        return _apply_deductive(criterion, output)
    if mode == "banded":
        return _apply_banded(criterion, output)
    return _apply_evidence_gate(output, criterion, chunk_outputs)


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
        label = str(band.get("label") or "")
        if label and (label == level or level in label or label in level):
            return band
    return None


def _numeric_bands(criterion):
    bands = []
    for band in getattr(criterion, "rubric_levels", None) or []:
        if isinstance(band, dict) and band.get("points") is not None:
            try:
                bands.append({"label": band.get("label") or str(band.get("points")), "points": float(band["points"])})
            except (TypeError, ValueError):
                continue
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
    )


def _aggregate_hybrid(criterion, sub_results, usage):
    max_score = float(criterion.max_score)
    awarded = round(min(sum(float(item.get("score") or 0) for item in sub_results), max_score), 2)
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
        "reason": "%s 按混合制核算：%d 个子检查合计 %.2f/%.2f。" % (criterion.name, len(sub_results), awarded, max_score),
        "deductions": _dedupe_texts(deductions),
        "deduction_items": _dedupe_deduction_items(deduction_items),
        "evidence": _dedupe_evidence(evidence)[:6],
        "suggestion": "混合制：确定性子项由规则核验，语义子项由模型评分。",
        "confidence": round(min(confidences), 3),
        "need_manual_review": any(item.get("need_manual_review") for item in sub_results),
        "scoring_mode": "hybrid",
        "sub_results": sub_results,
        "usage": usage,
    }


def _score_with_runtime_fallback(scorer, paper, criterion, candidates, structure_checks, rubric_version=None, anchors=None):
    provider = getattr(scorer, "provider", "")
    cache_request = None
    cache_key = None
    # L0 缓存（设计§7）：仅对真实模型生效；mock 廉价且确定，不缓存。
    if settings.LLM_CACHE_ENABLED and provider != "mock":
        cache_request = llm_cache.build_request(scorer, criterion, candidates, structure_checks, rubric_version, anchors)
        cache_key = llm_cache.key_of(cache_request)
        cached = llm_cache.get(cache_key)
        if cached is not None:
            cached = dict(cached)
            cached["cache_hit"] = True
            cached["cache_key"] = cache_key
            cached["usage"] = _blank_usage()  # 命中不计新增 token 成本
            return cached

    try:
        _throttle_real_llm_call(scorer)
        output = scorer.score_criterion(paper, criterion, candidates, structure_checks, anchors)
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
        llm_cache.put(cache_key, cache_request, output, model=getattr(scorer, "model_name", ""))
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
    weighted_score = 0
    total_weight = 0
    for output in chunk_outputs:
        confidence = float(output.get("confidence") or 0.5)
        weight = confidence if output.get("evidence_sufficient") else confidence * 0.6
        weighted_score += float(output["score"]) * weight
        total_weight += weight

    score = round(weighted_score / total_weight, 2) if total_weight else 0
    confidence = round(sum(float(output.get("confidence") or 0) for output in chunk_outputs) / len(chunk_outputs), 3)
    evidence = []
    deductions = []
    deduction_items = []
    chunk_scores = []
    need_review = False
    evidence_sufficient = False
    for output in chunk_outputs:
        evidence_sufficient = evidence_sufficient or bool(output.get("evidence_sufficient"))
        need_review = need_review or bool(output.get("need_manual_review"))
        deductions.extend(output.get("deductions") or [])
        deduction_items.extend(output.get("deduction_items") or [])
        evidence.extend(output.get("evidence") or [])
        chunk_scores.append(
            {
                "chunk_id": output.get("chunk_id"),
                "location": output.get("chunk_location"),
                "score": output.get("score"),
                "max_score": max_score,
                "confidence": output.get("confidence"),
                "evidence_sufficient": output.get("evidence_sufficient"),
            }
        )

    deductions = _dedupe_texts(deductions)
    deduction_items = _dedupe_deduction_items(deduction_items)
    evidence = _dedupe_evidence(evidence)[:6]
    score = max(0, min(score, max_score))
    return {
        "criterion_id": criterion.id,
        "criterion_name": criterion.name,
        "max_score": max_score,
        "score": score,
        "evidence_sufficient": evidence_sufficient,
        "reason": _aggregate_reason(criterion.name, score, max_score, chunk_scores),
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


def _short_error(exc):
    text = str(exc).strip() or exc.__class__.__name__
    return " ".join(text.split())[:240]


def _recalculate_run(run, items, parse_quality, total_score):
    total = calculate_total_score(items, total_score=total_score)
    run.ai_total_score = calculate_total_score(_AiScoreProxyList(items), total_score=total_score)
    run.final_total_score = total
    run.grade = match_grade(total)
    run.need_manual_review = need_manual_review(total, items, parse_quality=parse_quality)


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
        actual = resolve_default_format(path)
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
