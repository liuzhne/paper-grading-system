"""P4 connection-local circuit breaker executable specification."""

from datetime import datetime
from datetime import timedelta

import pytest

from backend.app.services.llm.rate_limit import CircuitOpenError
from backend.app.services.llm.rate_limit import ConnectionCircuitBreaker


def test_auth_failure_opens_until_explicit_reset():
    now = datetime(2026, 9, 3, 0, 0, 0)
    breaker = ConnectionCircuitBreaker(
        transient_threshold=3,
        cooldown_seconds=10,
    )

    breaker.record_failure("authentication_failed", now=now)
    with pytest.raises(CircuitOpenError):
        breaker.before_request(now=now + timedelta(hours=1))

    breaker.reset()
    permit = breaker.before_request(now=now + timedelta(hours=1))
    breaker.record_success(permit=permit)
    assert breaker.snapshot()["state"] == "closed"


def test_transient_threshold_half_open_and_successful_recovery():
    now = datetime(2026, 9, 3, 0, 0, 0)
    breaker = ConnectionCircuitBreaker(
        transient_threshold=2,
        cooldown_seconds=10,
    )
    breaker.record_failure("rate_limited", now=now)
    breaker.record_failure("provider_unavailable", now=now)

    with pytest.raises(CircuitOpenError):
        breaker.before_request(now=now + timedelta(seconds=5))
    probe = breaker.before_request(now=now + timedelta(seconds=11))
    assert probe.half_open_probe is True
    with pytest.raises(CircuitOpenError):
        breaker.before_request(now=now + timedelta(seconds=11))

    breaker.record_success(permit=probe)
    assert breaker.snapshot()["state"] == "closed"


def test_non_transient_probe_failure_closes_instead_of_sticking_half_open():
    # A probe answered with HTTP 400 proves the provider is reachable.  The
    # breaker used to stay half-open, admitting one request at a time and
    # failing every concurrent one with PROVIDER_CIRCUIT_OPEN indefinitely.
    now = datetime(2026, 9, 3, 0, 0, 0)
    breaker = ConnectionCircuitBreaker(
        transient_threshold=1,
        cooldown_seconds=10,
    )
    breaker.record_failure("network_error", now=now)
    probe = breaker.before_request(now=now + timedelta(seconds=11))
    assert probe.half_open_probe is True

    breaker.record_failure("invalid_request", permit=probe, now=now)

    assert breaker.snapshot()["state"] == "closed"
    first = breaker.before_request(now=now + timedelta(seconds=12))
    second = breaker.before_request(now=now + timedelta(seconds=12))
    assert first.half_open_probe is False
    assert second.half_open_probe is False


def test_non_transient_failure_resets_consecutive_transient_count():
    now = datetime(2026, 9, 3, 0, 0, 0)
    breaker = ConnectionCircuitBreaker(
        transient_threshold=2,
        cooldown_seconds=10,
    )
    breaker.record_failure("network_error", now=now)
    breaker.record_failure("invalid_request", now=now)
    breaker.record_failure("network_error", now=now)

    assert breaker.snapshot()["state"] == "closed"
    assert breaker.snapshot()["transient_failures"] == 1


def test_unknown_probe_failure_reopens_instead_of_closing():
    now = datetime(2026, 9, 3, 0, 0, 0)
    breaker = ConnectionCircuitBreaker(
        transient_threshold=1,
        cooldown_seconds=10,
    )
    breaker.record_failure("network_error", now=now)
    probe_at = now + timedelta(seconds=11)
    probe = breaker.before_request(now=probe_at)

    # No HTTP response, so reachability is unproven: back to a full cooldown.
    breaker.record_failure("unknown", permit=probe, now=probe_at)

    assert breaker.snapshot()["state"] == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_request(now=probe_at + timedelta(seconds=5))
    assert breaker.before_request(
        now=probe_at + timedelta(seconds=11)
    ).half_open_probe is True


def test_circuit_key_changes_with_key_version_and_model():
    # A model's exhausted free quota (HTTP 403) opens its circuit permanently;
    # switching the model or rotating the key must start from a fresh circuit.
    from backend.app.services.llm.rate_limit import provider_circuit_key

    snapshot = {"ai_connection_id": "conn-1", "key_version": 1, "model_name": "kimi-k3"}
    same = provider_circuit_key(snapshot, base_url="https://x/v1", model_name="kimi-k3")
    other_model = provider_circuit_key(
        {**snapshot, "model_name": "qwen3-max"}, base_url="https://x/v1", model_name="qwen3-max"
    )
    rotated = provider_circuit_key(
        {**snapshot, "key_version": 2}, base_url="https://x/v1", model_name="kimi-k3"
    )

    assert len({same, other_model, rotated}) == 3
    assert same == provider_circuit_key(
        dict(snapshot), base_url="https://y/v1", model_name="ignored"
    )
    assert provider_circuit_key(None, base_url="https://x/v1", model_name="m") == (
        "https://x/v1|m"
    )
