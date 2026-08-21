"""Regression tests for the M1 cache response type boundary."""

from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

from backend.app.core.config import settings
from backend.app.services.cache import llm_cache
from backend.app.services.scoring.core.contracts import PromptEnvelopeV1
from backend.app.services.scoring.engine import _score_with_runtime_fallback
from backend.app.tests.m1_contract_helpers import prompt_envelope_payload


def _envelope(*, provider="mock", model="mock-criterion-scorer", model_version="v1"):
    payload = prompt_envelope_payload()
    payload["provider"].update(
        {"name": provider, "model": model, "model_version": model_version}
    )
    return PromptEnvelopeV1.from_mapping(payload)


def test_cache_ledger_treats_valid_non_object_json_response_as_miss(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path)
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    envelope = _envelope()
    key = llm_cache.put_envelope(
        envelope=envelope,
        response={"score": 8},
        policy=llm_cache.CacheRetentionPolicy(
            retention_seconds=3600,
            store_controlled_original=False,
            allowed_scopes=("scoring-runtime",),
        ),
        access_scope="scoring-runtime",
        now=now,
    )
    with sqlite3.connect(str(tmp_path / "llm_cache.sqlite")) as connection:
        connection.execute(
            "UPDATE llm_envelope_cache SET response = ? WHERE key = ?",
            ("1", key),
        )
        connection.commit()

    assert (
        llm_cache.get_entry(key, requester_scope="scoring-runtime", now=now) is None
    )


def test_runtime_ignores_non_mapping_cache_entry_and_calls_provider(monkeypatch):
    envelope = _envelope(provider="cache-test", model="cache-model")
    calls = []

    class Scorer:
        provider = "cache-test"
        model_name = "cache-model"
        model_version = "v1"

        def build_prompt_envelope(self, *_args, **_kwargs):
            return envelope

        def score_envelope(self, provider_envelope):
            calls.append(provider_envelope)
            return {"score": 8, "usage": {}}

    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_MOCK", False)
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_SLEEP_SECONDS", 0)
    monkeypatch.setattr(
        llm_cache,
        "get_entry",
        lambda *_args, **_kwargs: SimpleNamespace(response=1),
    )
    monkeypatch.setattr(llm_cache, "put_envelope", lambda **_kwargs: None)

    result = _score_with_runtime_fallback(
        Scorer(),
        paper=object(),
        criterion=object(),
        candidates=[],
        structure_checks=[],
    )

    assert result["score"] == 8
    assert calls == [envelope]
