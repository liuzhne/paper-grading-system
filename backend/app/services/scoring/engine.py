from datetime import datetime
from datetime import timezone
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
from backend.app.services.checkers import run_deterministic_checker
from backend.app.services.retrieval.keyword import retrieve_for_criterion
from backend.app.services.scoring.rules import calculate_total_score
from backend.app.services.scoring.rules import match_grade
from backend.app.services.scoring.rules import need_manual_review
from backend.app.services.scoring.validator import validate_score_output
from backend.app.services.storage.local import read_json


def score_paper(db: Session, paper_id: str, scorer=None):
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

    rubric = paper.batch.rubric
    parsed = read_json(paper.parsed_text_path)
    structure_checks = parsed.get("structure_checks", [])
    run = ScoringRun(
        paper_id=paper.id,
        rubric_id=rubric.id,
        model_provider=scorer.provider,
        model_name=scorer.model_name,
        model_version=scorer.model_version,
        status="scoring",
        started_at=_utcnow(),
    )
    db.add(run)
    db.flush()

    items = []
    usage_totals = _blank_usage()
    for criterion in rubric.criteria:
        # 按 criterion_type 路由（设计§6.2）：deterministic 走确定性检查器（不调 LLM）；
        # llm_judgment/hybrid 走模型。hybrid 暂按 llm 路径，子检查拆分见后续阶段。
        criterion_type = getattr(criterion, "criterion_type", "llm_judgment")
        if criterion_type == "deterministic":
            output = run_deterministic_checker(criterion, parsed)
        elif criterion_type == "hybrid" and getattr(criterion, "sub_checks", None):
            output = _score_hybrid(db, scorer, paper, criterion, parsed, structure_checks, rubric.version)
        else:
            candidates = retrieve_for_criterion(db, paper.id, criterion, top_k=settings.SCORING_CHUNK_EVAL_TOP_K)
            output = _score_criterion_by_chunks(scorer, paper, criterion, candidates, structure_checks, rubric.version)
        _add_usage(usage_totals, output.get("usage"))
        item = ScoreItem(
            scoring_run_id=run.id,
            criterion_id=criterion.id,
            max_score=criterion.max_score,
            ai_score=output["score"],
            final_score=output["score"],
            evidence_sufficient=output["evidence_sufficient"],
            reason=output["reason"],
            deductions=output["deductions"],
            deduction_items=output.get("deduction_items") or [],
            evidence=output["evidence"],
            band_selection=output.get("band_selection"),
            sub_results=output.get("sub_results"),
            suggestion=output["suggestion"],
            confidence=output["confidence"],
            need_manual_review=output["need_manual_review"],
            raw_model_output=output,
        )
        db.add(item)
        items.append(item)

    db.flush()
    run.prompt_tokens = usage_totals["prompt_tokens"]
    run.completion_tokens = usage_totals["completion_tokens"]
    run.total_tokens = usage_totals["total_tokens"]
    _recalculate_run(run, items, paper.parse_quality, rubric.total_score)
    run.status = "scored"
    run.finished_at = _utcnow()
    paper.status = "pending_review" if run.need_manual_review else "scored"
    db.commit()
    db.refresh(run)
    return run


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


def _score_criterion_by_chunks(scorer, paper, criterion, candidates, structure_checks, rubric_version):
    if not candidates:
        raw_output = _score_with_runtime_fallback(scorer, paper, criterion, [], structure_checks, rubric_version)
        output = validate_score_output(raw_output, criterion, [])
        output["chunk_evaluation_mode"] = "single-empty-evidence"
        output["chunk_scores"] = []
        chunk_outputs = []
    else:
        chunk_outputs = []
        for index, candidate in enumerate(candidates, start=1):
            raw_output = _score_with_runtime_fallback(scorer, paper, criterion, [candidate], structure_checks, rubric_version)
            chunk_output = validate_score_output(raw_output, criterion, [candidate])
            chunk_output["chunk_index"] = index
            chunk_output["chunk_id"] = candidate.get("chunk_id")
            chunk_output["chunk_location"] = candidate.get("location") or candidate.get("section_title") or ""
            chunk_outputs.append(chunk_output)
        output = _aggregate_chunk_outputs(criterion, chunk_outputs)

    # 计分模式分流（设计§6.3 / N6）：deductive 代码算分跳过封顶；banded 吸附到档位；其余走证据门槛。
    mode = getattr(criterion, "scoring_mode", "llm_direct")
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
    """分档制（设计§6.3）：把模型判断吸附到 rubric_levels 中最接近的档位分，并记录带证据的 BandSelection。
    注：当前按模型给分就近吸附；让模型直接选档的 banded 专属 prompt 属后续增强。"""
    bands = _numeric_bands(criterion)
    if not bands:
        # 无有效档位 → 退回证据门槛，避免分档项无法计分。
        return _apply_evidence_gate(output, criterion, [])
    max_score = float(criterion.max_score)
    model_score = float(output.get("score") or 0)
    chosen = min(bands, key=lambda band: (abs(band["points"] - model_score), -band["points"]))
    first_evidence = (output.get("evidence") or [{}])[0] if output.get("evidence") else {}
    output["score_before_banded"] = model_score
    output["score"] = round(min(float(chosen["points"]), max_score), 2)
    output["band_selection"] = {
        "level": chosen.get("label"),
        "awarded": output["score"],
        "rationale": output.get("reason") or "",
        "rule_ref": getattr(criterion, "code", None),
        "evidence_location": first_evidence.get("location", ""),
        "evidence_quote": first_evidence.get("quote", ""),
    }
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


def _numeric_bands(criterion):
    bands = []
    for band in getattr(criterion, "rubric_levels", None) or []:
        if isinstance(band, dict) and band.get("points") is not None:
            try:
                bands.append({"label": band.get("label") or str(band.get("points")), "points": float(band["points"])})
            except (TypeError, ValueError):
                continue
    return bands


def _score_hybrid(db, scorer, paper, criterion, parsed, structure_checks, rubric_version):
    """混合制（设计§2/§6.3）：按 sub_checks 拆成确定性/语义子检查分别计分，再汇总。
    确定性子项走 checker（不调 LLM），语义子项走模型；本项得分 = Σ 子项得分。"""
    sub_results = []
    usage = _blank_usage()
    for index, sub in enumerate(criterion.sub_checks, start=1):
        sub_criterion = _make_sub_criterion(criterion, sub, index)
        if sub_criterion.criterion_type == "deterministic":
            sub_output = run_deterministic_checker(sub_criterion, parsed)
        else:
            candidates = retrieve_for_criterion(db, paper.id, sub_criterion, top_k=settings.SCORING_CHUNK_EVAL_TOP_K)
            sub_output = _score_criterion_by_chunks(scorer, paper, sub_criterion, candidates, structure_checks, rubric_version)
        _add_usage(usage, sub_output.get("usage"))
        sub_results.append(sub_output)
    return _aggregate_hybrid(criterion, sub_results, usage)


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


def _score_with_runtime_fallback(scorer, paper, criterion, candidates, structure_checks, rubric_version=None):
    provider = getattr(scorer, "provider", "")
    cache_request = None
    cache_key = None
    # L0 缓存（设计§7）：仅对真实模型生效；mock 廉价且确定，不缓存。
    if settings.LLM_CACHE_ENABLED and provider != "mock":
        cache_request = llm_cache.build_request(scorer, criterion, candidates, structure_checks, rubric_version)
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
        output = scorer.score_criterion(paper, criterion, candidates, structure_checks)
    except Exception as exc:
        if not settings.LLM_FALLBACK_TO_MOCK or provider == "mock":
            raise LLMScoringError("真实模型调用失败：%s" % _short_error(exc)) from exc

        fallback = MockLLMScorer()
        output = fallback.score_criterion(paper, criterion, candidates, structure_checks)
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
