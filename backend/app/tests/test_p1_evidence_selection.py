"""P1 executable specification for bounded, rule-scoped provider input."""

from copy import deepcopy

import pytest

from backend.app.services.cache import llm_cache
from backend.app.services.scoring.core.contracts import PromptEnvelopeV3
from backend.app.services.scoring.core.contracts import PromptEnvelopeV4
from backend.app.services.scoring.core.rule_executor import _prompt_envelope
from backend.app.services.scoring.retrieval.selection import TokenBudgetError
from backend.app.services.scoring.retrieval.selection import build_v4_envelope
from backend.app.services.scoring.retrieval.selection import conservative_token_estimate
from backend.app.services.scoring.retrieval.selection import preflight_v4_provider_payload
from backend.app.services.scoring.profiles.technical_proposal import TechnicalProposalProfile
from backend.app.core.config import settings
from backend.app.tests.m3_contract_fixtures import SEMANTIC_RULE_CODE
from backend.app.tests.m3_contract_fixtures import scoring_request_payload


class _Profile:
    profile_key = "technical_proposal"
    profile_version = "technical-proposal-profile@1"
    prompt_version = "technical-proposal-prompt@1"

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        return {"metadata": {}, "profile_extensions": {}}


def _v3():
    request = scoring_request_payload()
    node = next(
        item
        for item in request["plan"]["nodes"]
        if item["rule_code"] == SEMANTIC_RULE_CODE
    )
    envelope = _prompt_envelope(request=request, node=node, profile=_Profile())
    assert isinstance(envelope, PromptEnvelopeV3)
    return envelope


def test_conservative_estimator_counts_cjk_without_underestimating_by_four():
    assert conservative_token_estimate("中文" * 100) >= 200


def test_v4_selects_rule_scoped_evidence_and_freezes_budget_identity():
    source = _v3()
    source_mapping = source.to_mapping()
    envelope = build_v4_envelope(
        source,
        context_window_tokens=4096,
        reserved_output_tokens=512,
        safety_margin_tokens=256,
        top_k=1,
    )

    assert isinstance(envelope, PromptEnvelopeV4)
    value = envelope.to_mapping()
    selection = value["evidence_selection_identity"]
    budget = value["token_budget_identity"]
    assert value["schema_version"] == "prompt-envelope@4"
    assert len(value["evidence_units"]) == 1
    assert selection["selected_evidence_unit_ids"] == [
        value["evidence_units"][0]["evidence_unit_id"]
    ]
    assert selection["criterion_code"] == "SOLUTION_FIT"
    assert selection["rule_code"] == SEMANTIC_RULE_CODE
    assert len(selection["selection_hash"]) == 64
    assert budget["within_budget"] is True
    assert budget["total_reserved_tokens"] <= budget["context_window_tokens"]
    assert source.to_mapping() == source_mapping  # published V3 remains immutable
    assert llm_cache.key_of(envelope) == envelope.canonical_hash()


def test_v4_never_truncates_authoritative_quote_to_fit_budget():
    source = _v3()
    with pytest.raises(TokenBudgetError, match="TOKEN_BUDGET_UNSATISFIABLE"):
        build_v4_envelope(
            source,
            context_window_tokens=64,
            reserved_output_tokens=32,
            safety_margin_tokens=16,
            top_k=1,
        )


def test_v4_contract_rejects_selection_identity_tampering():
    envelope = build_v4_envelope(
        _v3(),
        context_window_tokens=4096,
        reserved_output_tokens=512,
        safety_margin_tokens=256,
        top_k=1,
    ).to_mapping()
    tampered = deepcopy(envelope)
    tampered["evidence_selection_identity"]["selected_evidence_unit_ids"] = []

    with pytest.raises(ValueError, match="selected evidence"):
        PromptEnvelopeV4.from_mapping(tampered)


def test_production_profile_builds_v4_and_keeps_explicit_v3_rollback(monkeypatch):
    profile = TechnicalProposalProfile()
    source = _v3()
    monkeypatch.setattr(settings, "SCORING_PROMPT_ENVELOPE_VERSION", "v4")
    monkeypatch.setattr(settings, "SCORING_EVIDENCE_TOP_K", 1)
    monkeypatch.setattr(settings, "SCORING_CONTEXT_WINDOW_TOKENS", 4096)
    monkeypatch.setattr(settings, "SCORING_CONTEXT_SAFETY_MARGIN_TOKENS", 256)

    scoped = profile.build_provider_envelope(base_envelope=source)
    assert isinstance(scoped, PromptEnvelopeV4)

    monkeypatch.setattr(settings, "SCORING_PROMPT_ENVELOPE_VERSION", "v3")
    rollback = profile.build_provider_envelope(base_envelope=source)
    assert rollback is source


def test_final_provider_preflight_accounts_for_system_instructions():
    envelope = build_v4_envelope(
        _v3(),
        context_window_tokens=4096,
        reserved_output_tokens=512,
        safety_margin_tokens=256,
        top_k=1,
    )

    with pytest.raises(TokenBudgetError, match="TOKEN_BUDGET_UNSATISFIABLE"):
        preflight_v4_provider_payload(envelope, "规则" * 10000)
