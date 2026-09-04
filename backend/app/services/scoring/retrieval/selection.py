"""Deterministic BM25 evidence selection and conservative token preflight."""

from __future__ import annotations

from collections import Counter
from collections import defaultdict
from copy import deepcopy
import json
import math
import re

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import PromptEnvelopeV3
from backend.app.services.scoring.core.contracts import PromptEnvelopeV4


SELECTOR_VERSION = "section-bm25-diverse@1"
TOKEN_POLICY_VERSION = "provider-context-budget@1"
TOKEN_ESTIMATOR_VERSION = "utf8-conservative@1"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")
_HIERARCHICAL_MARKERS = (
    "一致",
    "闭环",
    "贯穿",
    "对应",
    "coherence",
    "consistent",
    "alignment",
)
_ABSENCE_MARKERS = (
    "缺失",
    "缺少",
    "不存在",
    "未包含",
    "missing",
    "absence",
    "required section",
)


class TokenBudgetError(ValueError):
    code = "TOKEN_BUDGET_UNSATISFIABLE"

    def __init__(self, message="minimum authorized evidence does not fit model context"):
        super().__init__(f"{self.code}: {message}")


def conservative_token_estimate(value) -> int:
    """Conservative fallback for providers without a local tokenizer.

    UTF-8 bytes divided by three avoids the common four-characters-per-token
    underestimate for CJK while remaining deliberately conservative for Latin
    text.  A fixed message/serialization allowance covers chat framing.
    """

    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(1, math.ceil(len(value.encode("utf-8")) / 3))


def _tokens(value: str) -> list[str]:
    raw = [item.casefold() for item in _TOKEN_RE.findall(value or "")]
    cjk = [item for item in raw if len(item) == 1 and "\u3400" <= item <= "\u9fff"]
    bigrams = [cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1)]
    return raw + bigrams


def _query_text(value: dict) -> str:
    criterion = value["criterion_snapshot"]
    rule = value["atomic_rule_snapshot"]
    parts = [
        criterion["criterion_code"],
        criterion["name"],
        rule["rule_code"],
        " ".join(rule["evidence_policy"].get("allowed_finding_codes", ()) or ()),
    ]
    for level in rule.get("levels", ()) or ():
        parts.extend(
            item
            for item in (
                level.get("descriptor"),
                level.get("positive_example"),
                level.get("negative_example"),
            )
            if item
        )
    return "\n".join(str(item) for item in parts if item)


def _coverage_mode(value: dict, query: str) -> str:
    rule = value["atomic_rule_snapshot"]
    searchable = (query + " " + rule["evidence_policy"]["mode"]).casefold()
    if any(marker in searchable for marker in _ABSENCE_MARKERS):
        return "exhaustive"
    if rule.get("depends_on_rule_codes") or any(
        marker in searchable for marker in _HIERARCHICAL_MARKERS
    ):
        return "hierarchical"
    return "top_k"


def _rank(units: list[dict], query: str) -> list[dict]:
    query_terms = Counter(_tokens(query))
    if not query_terms:
        return list(units)
    documents = []
    document_frequency = Counter()
    for unit in units:
        heading = " ".join(unit["section_path"])
        terms = _tokens(heading + "\n" + unit["normalized_text"])
        counts = Counter(terms)
        documents.append((unit, counts, len(terms), set(_tokens(heading))))
        document_frequency.update(counts.keys())
    average_length = sum(item[2] for item in documents) / max(len(documents), 1)
    ranked = []
    for ordinal, (unit, counts, length, heading_terms) in enumerate(documents):
        score = 0.0
        for term, query_weight in query_terms.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(
                1
                + (len(documents) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.2 * (
                1 - 0.75 + 0.75 * length / max(average_length, 1)
            )
            score += query_weight * inverse * frequency * 2.2 / denominator
            if term in heading_terms:
                score += query_weight * 2.0
        ranked.append((score, -ordinal, unit))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]


def _diversify(units: list[dict]) -> list[dict]:
    by_section = defaultdict(list)
    section_order = []
    for unit in units:
        key = tuple(unit["section_path"])
        if key not in by_section:
            section_order.append(key)
        by_section[key].append(unit)
    result = []
    depth = 0
    while True:
        added = False
        for key in section_order:
            values = by_section[key]
            if depth < len(values):
                result.append(values[depth])
                added = True
        if not added:
            return result
        depth += 1


def _section_coverage(all_units, selected):
    available = Counter(tuple(item["section_path"]) for item in all_units)
    chosen = Counter(tuple(item["section_path"]) for item in selected)
    return [
        {
            "section_path": list(path),
            "available_unit_count": available[path],
            "selected_unit_count": chosen[path],
            "complete": chosen[path] == available[path],
        }
        for path in sorted(available)
        if chosen[path]
    ]


def _build_mapping(base, selected, *, coverage_mode, context_window_tokens, reserved_output_tokens, safety_margin_tokens, estimated_input_tokens):
    criterion = base["criterion_snapshot"]
    rule = base["atomic_rule_snapshot"]
    query = _query_text(base)
    selection_projection = {
        "schema_version": "evidence-selection-snapshot@1",
        "selector_version": SELECTOR_VERSION,
        "criterion_code": criterion["criterion_code"],
        "rule_code": rule["rule_code"],
        "query_hash": canonical_sha256({"query": query}),
        "selected_evidence_unit_ids": [item["evidence_unit_id"] for item in selected],
        "section_coverage": _section_coverage(base["evidence_units"], selected),
    }
    selection_hash = canonical_sha256(
        {**selection_projection, "coverage_mode": coverage_mode}
    )
    total = estimated_input_tokens + reserved_output_tokens + safety_margin_tokens
    budget_projection = {
        "schema_version": "token-budget@1",
        "policy_version": TOKEN_POLICY_VERSION,
        "estimator_version": TOKEN_ESTIMATOR_VERSION,
        "context_window_tokens": context_window_tokens,
        "reserved_output_tokens": reserved_output_tokens,
        "safety_margin_tokens": safety_margin_tokens,
        "estimated_input_tokens": estimated_input_tokens,
        "total_reserved_tokens": total,
        "within_budget": total <= context_window_tokens,
    }
    return {
        **deepcopy(base),
        "schema_version": "prompt-envelope@4",
        "evidence_units": deepcopy(selected),
        "evidence_selection_identity": {
            **selection_projection,
            "selection_hash": selection_hash,
        },
        "token_budget_identity": {
            **budget_projection,
            "policy_hash": canonical_sha256(budget_projection),
        },
        "coverage_mode": coverage_mode,
    }


def build_v4_envelope(
    envelope,
    *,
    context_window_tokens: int,
    reserved_output_tokens: int,
    safety_margin_tokens: int,
    top_k: int,
):
    """Select whole authoritative units and prove the final request fits."""

    base = PromptEnvelopeV3.from_mapping(envelope).to_mapping()
    for name, value, minimum in (
        ("context_window_tokens", context_window_tokens, 1),
        ("reserved_output_tokens", reserved_output_tokens, 1),
        ("safety_margin_tokens", safety_margin_tokens, 0),
        ("top_k", top_k, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    query = _query_text(base)
    coverage_mode = _coverage_mode(base, query)
    ranked = _diversify(_rank(list(base["evidence_units"]), query))
    selected = ranked if coverage_mode == "exhaustive" else ranked[:top_k]
    if not selected:
        raise TokenBudgetError("no authorized evidence is available")

    while selected:
        estimated = 1
        candidate = None
        # The recorded estimate is part of the JSON being estimated.  Iterate
        # to a fixed point so the final identity describes the final payload.
        for _ in range(4):
            candidate = _build_mapping(
                base,
                selected,
                coverage_mode=coverage_mode,
                context_window_tokens=context_window_tokens,
                reserved_output_tokens=reserved_output_tokens,
                safety_margin_tokens=safety_margin_tokens,
                estimated_input_tokens=estimated,
            )
            # Reserve conservative chat framing and system-instruction space.
            # The adapter performs a second exact-payload preflight below.
            measured = conservative_token_estimate(candidate) + 1024
            if measured == estimated:
                break
            estimated = measured
        candidate = _build_mapping(
            base,
            selected,
            coverage_mode=coverage_mode,
            context_window_tokens=context_window_tokens,
            reserved_output_tokens=reserved_output_tokens,
            safety_margin_tokens=safety_margin_tokens,
            estimated_input_tokens=estimated,
        )
        total = estimated + reserved_output_tokens + safety_margin_tokens
        if total <= context_window_tokens:
            return PromptEnvelopeV4.from_mapping(candidate)
        if coverage_mode == "exhaustive":
            break
        selected = selected[:-1]
    raise TokenBudgetError()


def preflight_v4_provider_payload(envelope, system_instructions: str) -> int:
    """Verify the exact serialized provider input immediately before I/O."""

    value = PromptEnvelopeV4.from_mapping(envelope).to_mapping()
    budget = value["token_budget_identity"]
    estimated = (
        conservative_token_estimate(value)
        + conservative_token_estimate(system_instructions)
        + 64
    )
    total = (
        estimated
        + budget["reserved_output_tokens"]
        + budget["safety_margin_tokens"]
    )
    if total > budget["context_window_tokens"]:
        raise TokenBudgetError("final serialized provider payload exceeds context budget")
    return estimated


__all__ = [
    "SELECTOR_VERSION",
    "TOKEN_ESTIMATOR_VERSION",
    "TOKEN_POLICY_VERSION",
    "TokenBudgetError",
    "build_v4_envelope",
    "conservative_token_estimate",
    "preflight_v4_provider_payload",
]
