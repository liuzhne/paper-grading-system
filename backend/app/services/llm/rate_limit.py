"""Process-local connection concurrency and provider circuit breaker.

The durable rule-task state remains authoritative.  This fast guard prevents
one application instance from amplifying an already-known provider incident;
cross-instance coordination can later use the same stable state contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import threading

from backend.app.core.config import settings
from backend.app.services.llm.errors import project_provider_error


_AUTH_CODES = {"authentication_failed", "permission_denied"}
_TRANSIENT_CODES = {
    "request_timeout",
    "rate_limited",
    "capacity_unavailable",
    "provider_unavailable",
    "network_error",
}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CircuitOpenError(RuntimeError):
    code = "PROVIDER_CIRCUIT_OPEN"

    def __init__(self):
        super().__init__("provider circuit is open; request deferred")


@dataclass(frozen=True)
class CircuitPermit:
    half_open_probe: bool = False


class ConnectionCircuitBreaker:
    def __init__(self, *, transient_threshold: int, cooldown_seconds: int):
        self.transient_threshold = int(transient_threshold)
        self.cooldown_seconds = int(cooldown_seconds)
        self._lock = threading.Lock()
        self._state = "closed"
        self._transient_failures = 0
        self._opened_until = None
        self._permanent = False
        self._probe_in_flight = False
        self._last_error_code = None

    def before_request(self, *, now=None) -> CircuitPermit:
        now = now or _now()
        with self._lock:
            if self._state == "closed":
                return CircuitPermit()
            if self._permanent or self._opened_until is None or now < self._opened_until:
                raise CircuitOpenError()
            if self._probe_in_flight:
                raise CircuitOpenError()
            self._state = "half_open"
            self._probe_in_flight = True
            return CircuitPermit(half_open_probe=True)

    def record_failure(self, code: str, *, permit=None, now=None):
        now = now or _now()
        normalized = str(code or "unknown")
        with self._lock:
            self._last_error_code = normalized
            if normalized in _AUTH_CODES:
                self._state = "open"
                self._permanent = True
                self._opened_until = None
                self._probe_in_flight = False
                return
            if normalized not in _TRANSIENT_CODES:
                if permit is not None and permit.half_open_probe:
                    self._probe_in_flight = False
                return
            self._transient_failures += 1
            if (
                permit is not None and permit.half_open_probe
            ) or self._transient_failures >= self.transient_threshold:
                self._state = "open"
                self._permanent = False
                self._opened_until = now + timedelta(seconds=self.cooldown_seconds)
                self._probe_in_flight = False

    def record_success(self, *, permit=None):
        del permit
        with self._lock:
            self._state = "closed"
            self._transient_failures = 0
            self._opened_until = None
            self._permanent = False
            self._probe_in_flight = False
            self._last_error_code = None

    def reset(self):
        self.record_success()

    def snapshot(self):
        with self._lock:
            return {
                "state": self._state,
                "transient_failures": self._transient_failures,
                "opened_until": self._opened_until,
                "permanent": self._permanent,
                "probe_in_flight": self._probe_in_flight,
                "last_error_code": self._last_error_code,
            }


class _ConnectionRuntime:
    def __init__(self):
        self.semaphore = threading.BoundedSemaphore(
            settings.PROVIDER_GLOBAL_CONCURRENCY
        )
        self.breaker = ConnectionCircuitBreaker(
            transient_threshold=settings.PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
            cooldown_seconds=settings.PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
        )


_registry = {}
_registry_lock = threading.Lock()


def _runtime(key):
    with _registry_lock:
        runtime = _registry.get(key)
        if runtime is None:
            runtime = _ConnectionRuntime()
            _registry[key] = runtime
        return runtime


class _DisabledSlot:
    def __enter__(self):
        return self

    def record_response(self, response):
        del response

    def __exit__(self, exc_type, exc, traceback):
        return False


class _ProviderRequestSlot:
    def __init__(self, runtime):
        self.runtime = runtime
        self.permit = None
        self.recorded = False

    def __enter__(self):
        self.permit = self.runtime.breaker.before_request()
        self.runtime.semaphore.acquire()
        return self

    def record_response(self, response):
        self.recorded = True
        try:
            response.raise_for_status()
        except Exception as exc:
            self.runtime.breaker.record_failure(
                project_provider_error(exc).code,
                permit=self.permit,
            )
        else:
            self.runtime.breaker.record_success(permit=self.permit)

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc is not None and not self.recorded:
                self.runtime.breaker.record_failure(
                    project_provider_error(exc).code,
                    permit=self.permit,
                )
            elif exc is None and not self.recorded:
                self.runtime.breaker.record_success(permit=self.permit)
        finally:
            self.runtime.semaphore.release()
        return False


def provider_request_slot(*, provider: str, connection_key: str):
    if not settings.PROVIDER_CIRCUIT_BREAKER_ENABLED:
        return _DisabledSlot()
    return _ProviderRequestSlot(_runtime((str(provider), str(connection_key))))


def reset_provider_runtime_for_tests():
    with _registry_lock:
        _registry.clear()


__all__ = [
    "CircuitOpenError",
    "CircuitPermit",
    "ConnectionCircuitBreaker",
    "provider_request_slot",
    "reset_provider_runtime_for_tests",
]
