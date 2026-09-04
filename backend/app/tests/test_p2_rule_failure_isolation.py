"""P2 executable specification: one provider failure cannot erase siblings."""

from copy import deepcopy

import httpx

from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.errors import project_provider_error
from backend.app.services.scoring.core.engine import score_submission
from backend.app.tests.m3_contract_fixtures import CHECKER_KEY
from backend.app.tests.m3_contract_fixtures import CHECKER_VERSION
from backend.app.tests.m3_contract_fixtures import DETERMINISTIC_RULE_CODE
from backend.app.tests.m3_contract_fixtures import SEMANTIC_RULE_CODE
from backend.app.tests.m3_contract_fixtures import deterministic_missing_owner_observation
from backend.app.tests.m3_contract_fixtures import scoring_request_payload
from backend.app.tests.m2_contract_fixtures import PROFILE_VERSION


class _Profile:
    profile_key = "technical_proposal"
    profile_version = PROFILE_VERSION
    prompt_version = "technical-proposal-prompt@1"

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        return {"metadata": {}, "profile_extensions": {}}


class _Registry:
    def resolve(self, **kwargs):
        assert kwargs["checker_key"] == CHECKER_KEY
        assert kwargs["checker_version"] == CHECKER_VERSION

        def checker(*, document, params):
            return deepcopy(deterministic_missing_owner_observation())

        return checker


class _FailingRuntime:
    def score(self, *, envelope):
        request = httpx.Request("POST", "https://api.groq.test/chat/completions")
        response = httpx.Response(
            503,
            request=request,
            json={"error": {"message": "provider unavailable"}},
        )
        raw = httpx.HTTPStatusError(
            "unsafe provider body", request=request, response=response
        )
        raise ProviderCallError("groq", project_provider_error(raw)) from raw


def test_provider_failure_becomes_one_invalid_rule_and_sibling_result_survives():
    outcome = score_submission(
        request=scoring_request_payload(),
        checker_registry=_Registry(),
        llm_runtime=_FailingRuntime(),
        profile=_Profile(),
    ).to_mapping()

    decisions = {item["rule_code"]: item for item in outcome["rule_decisions"]}
    assert outcome["status"] == "blocked"
    assert decisions[DETERMINISTIC_RULE_CODE]["status"] == "triggered"
    assert decisions[SEMANTIC_RULE_CODE]["status"] == "invalid"
    assert any(
        item["code"] == "PROVIDER_UNAVAILABLE"
        and item["rule_code"] == SEMANTIC_RULE_CODE
        for item in outcome["review_issues"]
    )
