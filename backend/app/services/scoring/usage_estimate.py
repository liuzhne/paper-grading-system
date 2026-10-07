"""Local estimate of Core semantic-rule input tokens, before any provider call.

The estimate assembles exactly the envelopes execution would send and sizes
them with the same conservative estimator as the send-time preflight
(``preflight_v4_provider_payload``).  Rules whose decision is already in the
rule decision ledger are counted as reused, not as calls.

Only the Core path is estimated.  Papers scored through the legacy path report
``supported=False`` instead of a misleading number.
"""

from __future__ import annotations

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.services.ai_connections import AIConnectionBindingError
from backend.app.services.scoring.core.decision_identity import rule_decision_identity
from backend.app.services.llm.core_view import build_core_group_request
from backend.app.services.llm.core_view import build_core_request
from backend.app.services.llm.core_view import request_input_estimate
from backend.app.services.scoring.core.rule_executor import eligible_rule_groups
from backend.app.services.scoring.core.rule_executor import group_decision_identity
from backend.app.services.scoring.core.rule_executor import plan_rule_order
from backend.app.services.scoring.core.rule_executor import prompt_envelope_for_rule
from backend.app.services.scoring.decision_ledger import DatabaseDecisionLedger
from backend.app.services.scoring.decision_ledger import connection_scope


class TokenCapExceededError(ValueError):
    """A job would exceed a configured input-token cap; nothing was queued."""

    def __init__(self, message, *, estimate):
        super().__init__(message)
        self.estimate = estimate


def envelope_input_estimate(envelope) -> int:
    """Size of the provider view actually sent for one rule (not the envelope)."""

    return request_input_estimate(build_core_request(envelope))


def _empty(paper, reason=None):
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "supported": False,
        "reason": reason,
        "semantic_rules": 0,
        "reused_rules": 0,
        "calls": 0,
        "estimated_input_tokens": 0,
    }


def estimate_paper(db, paper, *, reuse=True) -> dict:
    from backend.app.services.scoring import engine as scoring_engine

    result = _empty(paper)
    if paper.status == "failed" or not paper.parsed_text_path:
        result["reason"] = "not_parsed"
        return result
    if not (
        scoring_engine._has_locked_formal_version(db, paper.id)
        or settings.SCORING_ENGINE_MODE == "core"
    ):
        result["reason"] = "legacy_path"
        return result
    try:
        scorer = scoring_engine._scorer_for_paper(db, paper.id)
    except AIConnectionBindingError as exc:
        result["reason"] = exc.code
        return result
    except (RuntimeError, ValueError):
        result["reason"] = "model_unavailable"
        return result
    try:
        request, _registry, profile, _rubric, _paper, _workflow = (
            scoring_engine._core_request_context(db, paper.id, scorer)
        )
        value = request.to_mapping()
        nodes_by_code, order = plan_rule_order(value)
        semantic_by_criterion = {}
        for code in order:
            node = nodes_by_code[code]
            if node["atomic_rule_snapshot"]["judge_type"] == "semantic":
                semantic_by_criterion.setdefault(node["criterion_code"], []).append(
                    node["atomic_rule_snapshot"]
                )
        # Mirror the executor: group calls only for scorers that support them.
        groups = (
            eligible_rule_groups(nodes_by_code, order)
            if callable(getattr(scorer, "score_core_group", None))
            else {}
        )
        units = []  # (identity, rule_count, estimated_tokens)
        seen_groups = set()
        for code in order:
            node = nodes_by_code[code]
            if node["atomic_rule_snapshot"]["judge_type"] != "semantic":
                continue
            criterion_rules = semantic_by_criterion.get(node["criterion_code"])
            group = groups.get(code)
            if group is not None:
                key, members = group
                if key in seen_groups:
                    continue
                member_nodes = [nodes_by_code[member] for member in members]
                rules = [item["atomic_rule_snapshot"] for item in member_nodes]
                envelopes = [
                    prompt_envelope_for_rule(
                        request=value,
                        node=item,
                        profile=profile,
                        selection_rules=rules,
                        criterion_rules=criterion_rules,
                    )
                    for item in member_nodes
                ]
                first = envelopes[0].to_mapping()["evidence_units"]
                if all(env.to_mapping()["evidence_units"] == first for env in envelopes):
                    seen_groups.add(key)
                    units.append(
                        (
                            group_decision_identity(key[1], envelopes),
                            len(rules),
                            request_input_estimate(
                                build_core_group_request(envelopes, group_code=key[1])
                            ),
                        )
                    )
                    continue
            envelope = prompt_envelope_for_rule(
                request=value, node=node, profile=profile, criterion_rules=criterion_rules
            )
            units.append(
                (rule_decision_identity(envelope), 1, envelope_input_estimate(envelope))
            )
        known = set()
        if (
            reuse
            and settings.SCORING_DECISION_LEDGER_ENABLED
            and str(getattr(scorer, "provider", "")) != "mock"
        ):
            known = DatabaseDecisionLedger(
                bind=db.get_bind(),
                organization_id=getattr(paper, "organization_id", None),
                scope=connection_scope(scorer),
                ttl_days=settings.SCORING_DECISION_LEDGER_TTL_DAYS,
            ).known_identities(identity for identity, _count, _tokens in units)
    except ValueError:
        result["reason"] = "not_scoreable"
        return result
    finally:
        scoring_engine._close_scorer(scorer)

    result["supported"] = True
    for identity, rule_count, tokens in units:
        result["semantic_rules"] += rule_count
        if identity in known:
            result["reused_rules"] += rule_count
            continue
        result["calls"] += 1
        result["estimated_input_tokens"] += tokens
    return result


def _needs_scoring(db, paper, *, rescore) -> bool:
    """Mirror the job executor: without rescore, complete runs are skipped."""

    from backend.app.services.batch_scoring.jobs import _run_has_complete_scores

    if rescore or not paper.scoring_runs:
        return True
    previous = max(paper.scoring_runs, key=lambda run: (run.created_at, run.id))
    return not _run_has_complete_scores(db, previous)


def estimate_batch(db, batch_id, *, rescore=False, paper_ids=None) -> dict:
    batch = db.get(GradingBatch, batch_id)
    if batch is None:
        raise ValueError("batch not found")
    wanted = None if paper_ids is None else set(paper_ids)
    papers = []
    skipped = 0
    for paper in sorted(batch.papers, key=lambda value: (value.created_at, value.id)):
        if wanted is not None and paper.id not in wanted:
            continue
        if not _needs_scoring(db, paper, rescore=rescore):
            skipped += 1
            continue
        papers.append(estimate_paper(db, paper, reuse=not rescore))

    per_paper_cap = int(settings.SCORING_MAX_INPUT_TOKENS_PER_PAPER or 0)
    batch_cap = int(settings.SCORING_MAX_INPUT_TOKENS_PER_BATCH or 0)
    total = sum(item["estimated_input_tokens"] for item in papers)
    violations = []
    if batch_cap and total > batch_cap:
        violations.append({"kind": "batch", "estimated": total, "cap": batch_cap})
    if per_paper_cap:
        violations.extend(
            {
                "kind": "paper",
                "paper_id": item["paper_id"],
                "title": item["title"],
                "estimated": item["estimated_input_tokens"],
                "cap": per_paper_cap,
            }
            for item in papers
            if item["estimated_input_tokens"] > per_paper_cap
        )
    return {
        "batch_id": batch_id,
        "rescore": bool(rescore),
        "paper_count": len(papers),
        "skipped_complete_papers": skipped,
        "unsupported_papers": sum(1 for item in papers if not item["supported"]),
        "calls": sum(item["calls"] for item in papers),
        "reused_rules": sum(item["reused_rules"] for item in papers),
        "estimated_input_tokens": total,
        "caps": {"per_paper": per_paper_cap, "per_batch": batch_cap},
        "violations": violations,
        "papers": papers,
    }


def _cap_message(estimate) -> str:
    parts = []
    for violation in estimate["violations"]:
        if violation["kind"] == "batch":
            parts.append(
                "本批预计 %d，上限 %d" % (violation["estimated"], violation["cap"])
            )
    papers = [item for item in estimate["violations"] if item["kind"] == "paper"]
    for violation in papers[:3]:
        parts.append(
            "《%s》预计 %d，单篇上限 %d"
            % (violation["title"] or violation["paper_id"], violation["estimated"], violation["cap"])
        )
    if len(papers) > 3:
        parts.append("另有 %d 篇超过单篇上限" % (len(papers) - 3))
    return (
        "预计输入 token 超过上限，未开始评分（%s）。"
        "可调高 SCORING_MAX_INPUT_TOKENS_PER_PAPER / SCORING_MAX_INPUT_TOKENS_PER_BATCH，"
        "或减少待评分论文后重试。" % "；".join(parts)
    )


def assert_within_token_caps(db, batch_id, *, rescore=False, paper_ids=None):
    """Refuse to queue work whose estimate exceeds a configured cap.

    With no cap configured nothing is estimated, so job creation stays as fast
    as before.  Returns the estimate when one was computed.
    """

    if not (
        settings.SCORING_MAX_INPUT_TOKENS_PER_PAPER
        or settings.SCORING_MAX_INPUT_TOKENS_PER_BATCH
    ):
        return None
    estimate = estimate_batch(db, batch_id, rescore=rescore, paper_ids=paper_ids)
    if estimate["violations"]:
        raise TokenCapExceededError(_cap_message(estimate), estimate=estimate)
    return estimate


__all__ = [
    "TokenCapExceededError",
    "assert_within_token_caps",
    "envelope_input_estimate",
    "estimate_batch",
    "estimate_paper",
]
