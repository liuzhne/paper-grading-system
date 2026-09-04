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
