"""M2 Core runtime-port contracts.

These are pre-implementation contracts, not integration tests.  The probes are
deliberately stdlib-only and fail open only for the exact target module (or a
missing parent package).  An import failure inside an existing implementation
must remain a collection error instead of being mislabeled as an expected M2
failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import inspect
from types import ModuleType

import pytest

from backend.app.services.scoring.core.contracts import PromptEnvelopeV1
from backend.app.tests.m1_contract_helpers import prompt_envelope_payload


PORTS_MODULE = "backend.app.services.scoring.core.ports"
PORT_NAMES = (
    "LLMRuntime",
    "CacheLedger",
    "EvidenceRetriever",
    "CheckerRegistry",
    "Clock",
)


class M2CapabilityUnavailable(RuntimeError):
    """The sole exception allowed to turn an M2 capability gate into XFAIL."""


@dataclass(frozen=True, slots=True)
class _CapabilityProbe:
    target: str
    module: ModuleType | None
    symbols: Mapping[str, object]
    missing_symbols: tuple[str, ...]
    import_error: ModuleNotFoundError | None = None

    def has(self, name: str) -> bool:
        return self.module is not None and name in self.symbols

    def require_module(self) -> ModuleType:
        if self.module is None:
            error = M2CapabilityUnavailable(f"M2 module {self.target} is unavailable")
            if self.import_error is not None:
                raise error from self.import_error
            raise error
        return self.module

    def require(self, name: str):
        if not self.has(name):
            missing = ", ".join(self.missing_symbols) or name
            raise M2CapabilityUnavailable(
                f"{self.target} must expose M2 capability: {missing}"
            )
        return self.symbols[name]


def _probe_module(target: str, required: tuple[str, ...]) -> _CapabilityProbe:
    try:
        module = importlib.import_module(target)
    except ModuleNotFoundError as exc:
        target_or_parent_is_missing = exc.name == target or (
            exc.name is not None and target.startswith(exc.name + ".")
        )
        if not target_or_parent_is_missing:
            raise
        return _CapabilityProbe(
            target=target,
            module=None,
            symbols={},
            missing_symbols=required,
            import_error=exc,
        )

    # Deliberately avoid getattr(): module-level __getattr__ and AttributeError
    # raised during import must not be converted into a capability XFAIL.
    namespace = vars(module)
    symbols = {name: namespace[name] for name in required if name in namespace}
    missing = tuple(name for name in required if name not in namespace)
    return _CapabilityProbe(
        target=target,
        module=module,
        symbols=symbols,
        missing_symbols=missing,
    )


_PORTS_PROBE = _probe_module(PORTS_MODULE, PORT_NAMES)


def _requires_port(name: str):
    return pytest.mark.xfail(
        condition=not _PORTS_PROBE.has(name),
        reason=f"M2 {PORTS_MODULE}.{name} is not implemented",
        raises=M2CapabilityUnavailable,
        strict=True,
    )


requires_ports_module = pytest.mark.xfail(
    condition=_PORTS_PROBE.module is None,
    reason=f"M2 {PORTS_MODULE} is not implemented",
    raises=M2CapabilityUnavailable,
    strict=True,
)


def _port_case(name: str):
    return pytest.param(name, id=name, marks=_requires_port(name))


PORT_CASES = tuple(_port_case(name) for name in PORT_NAMES)


@pytest.fixture()
def port_contract(request):
    """Explicit capability requirement used by every per-port test."""

    name = str(request.param)
    return name, _PORTS_PROBE.require(name)


@pytest.fixture()
def ports_module():
    """Explicit module capability requirement for the boundary test."""

    return _PORTS_PROBE.require_module()


PORT_OPERATIONS = {
    "LLMRuntime": ("score",),
    "CacheLedger": ("get", "put"),
    "EvidenceRetriever": ("retrieve",),
    "CheckerRegistry": ("resolve", "manifest"),
    "Clock": ("now",),
}


class RecordingLLMRuntime:
    def __init__(self):
        self.envelopes: list[PromptEnvelopeV1] = []

    def score(self, *, envelope: PromptEnvelopeV1) -> Mapping[str, object]:
        self.envelopes.append(envelope)
        return {"verdict": "satisfied", "evidence": []}


class InMemoryCacheLedger:
    def __init__(self):
        self.entries: dict[tuple[str, str], tuple[object, datetime]] = {}

    def get(
        self,
        *,
        cache_key: str,
        requester_scope: str,
        now: datetime,
    ) -> object | None:
        entry = self.entries.get((cache_key, requester_scope))
        if entry is None:
            return None
        response, expires_at = entry
        return deepcopy(response) if now < expires_at else None

    def put(
        self,
        *,
        cache_key: str,
        response: object,
        access_scope: str,
        expires_at: datetime,
    ) -> None:
        self.entries[(cache_key, access_scope)] = (deepcopy(response), expires_at)


class StaticEvidenceRetriever:
    def __init__(self, evidence_units: tuple[Mapping[str, object], ...]):
        self.evidence_units = deepcopy(evidence_units)

    def retrieve(
        self,
        *,
        document: Mapping[str, object],
        query: Mapping[str, object],
        limit: int,
    ) -> tuple[Mapping[str, object], ...]:
        del document, query
        return deepcopy(self.evidence_units[:limit])


class InMemoryCheckerRegistry:
    def __init__(self, entries: Mapping[tuple[str, str], object]):
        self.entries = dict(entries)

    def resolve(self, *, checker_key: str, checker_version: str) -> object:
        return self.entries[(checker_key, checker_version)]

    def manifest(
        self,
        *,
        profile_key: str,
        document_schema_version: str,
    ) -> Mapping[str, Mapping[str, object]]:
        return {
            key: {
                "checker_version": version,
                "implementation_hash": "c" * 64,
                "params_schema": "checker-params@1",
                "supported_profiles": [profile_key],
                "supported_document_schemas": [document_schema_version],
                "observation_schema": "deterministic-observation@1",
            }
            for key, version in sorted(self.entries)
        }


class FixedClock:
    def __init__(self, instant: datetime):
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def _fake_for(name: str):
    if name == "LLMRuntime":
        return RecordingLLMRuntime()
    if name == "CacheLedger":
        return InMemoryCacheLedger()
    if name == "EvidenceRetriever":
        return StaticEvidenceRetriever(
            (
                {
                    "evidence_unit_id": "e" * 64,
                    "text": "风险章节明确给出了负责人和缓解措施。",
                },
            )
        )
    if name == "CheckerRegistry":
        return InMemoryCheckerRegistry(
            {("technical_proposal.required_sections.v1", "1.0.0"): object()}
        )
    if name == "Clock":
        return FixedClock(datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc))
    raise AssertionError(f"unknown test port {name}")


@pytest.mark.parametrize("port_contract", PORT_CASES, indirect=True)
def test_ports_are_protocols_with_structural_in_memory_substitutes(port_contract):
    name, protocol = port_contract
    assert inspect.isclass(protocol), f"{name} must be a Protocol class"
    assert getattr(protocol, "_is_protocol", False), f"{name} must extend Protocol"
    fake = _fake_for(name)
    assert all(callable(getattr(fake, method)) for method in PORT_OPERATIONS[name])
    if getattr(protocol, "_is_runtime_protocol", False):
        assert isinstance(fake, protocol)


@pytest.mark.parametrize("port_contract", PORT_CASES, indirect=True)
def test_ports_declare_only_the_m2_operations_with_typed_boundaries(port_contract):
    name, protocol = port_contract
    public_protocol_methods = {
        member_name
        for member_name, value in vars(protocol).items()
        if not member_name.startswith("_") and callable(value)
    }
    assert set(PORT_OPERATIONS[name]).issubset(public_protocol_methods)

    for method_name in PORT_OPERATIONS[name]:
        method = vars(protocol).get(method_name)
        assert callable(method), f"{name} must declare {method_name}()"
        signature = inspect.signature(method)
        parameters = tuple(signature.parameters.values())
        assert parameters and parameters[0].name == "self"
        assert all(
            parameter.annotation is not inspect.Signature.empty
            for parameter in parameters[1:]
        )
        assert signature.return_annotation is not inspect.Signature.empty

    if name == "LLMRuntime":
        score_parameters = tuple(
            inspect.signature(vars(protocol)["score"]).parameters.values()
        )[1:]
        assert len(score_parameters) == 1
        annotation = repr(score_parameters[0].annotation)
        assert "PromptEnvelopeV1" in annotation


@pytest.mark.parametrize("port_contract", PORT_CASES, indirect=True)
def test_pure_in_memory_fakes_are_operational_substitutes(port_contract):
    name, protocol = port_contract
    fake = _fake_for(name)
    if getattr(protocol, "_is_runtime_protocol", False):
        assert isinstance(fake, protocol)

    now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    if name == "LLMRuntime":
        envelope = PromptEnvelopeV1.from_mapping(prompt_envelope_payload())
        assert fake.score(envelope=envelope)["verdict"] == "satisfied"
        assert fake.envelopes[0] is envelope
        assert fake.envelopes[0].canonical_hash() == envelope.canonical_hash()
    elif name == "CacheLedger":
        fake.put(
            cache_key="a" * 64,
            response={"verdict": "satisfied"},
            access_scope="scoring-runtime",
            expires_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        )
        assert fake.get(
            cache_key="a" * 64,
            requester_scope="scoring-runtime",
            now=now,
        ) == {"verdict": "satisfied"}
        assert fake.get(
            cache_key="a" * 64,
            requester_scope="audit-ui",
            now=now,
        ) is None
    elif name == "EvidenceRetriever":
        result = fake.retrieve(document={}, query={"terms": ("风险",)}, limit=1)
        assert result[0]["evidence_unit_id"] == "e" * 64
    elif name == "CheckerRegistry":
        checker = fake.resolve(
            checker_key="technical_proposal.required_sections.v1",
            checker_version="1.0.0",
        )
        assert checker is not None
        manifest = fake.manifest(
            profile_key="technical_proposal",
            document_schema_version="document-snapshot@1",
        )
        assert manifest["technical_proposal.required_sections.v1"]["checker_version"] == "1.0.0"
    else:
        assert fake.now() == now
        assert fake.now().tzinfo is timezone.utc


@requires_ports_module
def test_ports_contract_does_not_leak_database_web_file_or_network_types(ports_module):
    banned_module_prefixes = (
        "sqlalchemy",
        "fastapi",
        "pathlib",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "backend.app.db",
        "backend.app.api",
        "backend.app.services.storage",
    )
    banned_parameter_fragments = (
        "db",
        "session",
        "path",
        "file",
        "url",
        "socket",
        "client",
    )

    leaked_public_objects = []
    for public_name, value in vars(ports_module).items():
        if public_name.startswith("_"):
            continue
        owner = value.__name__ if isinstance(value, ModuleType) else getattr(value, "__module__", "")
        if owner.startswith(banned_module_prefixes):
            leaked_public_objects.append(f"{public_name}:{owner}")
    assert leaked_public_objects == []

    for name in PORT_NAMES:
        if not _PORTS_PROBE.has(name):
            continue
        protocol = _PORTS_PROBE.require(name)
        for method_name in PORT_OPERATIONS[name]:
            signature = inspect.signature(vars(protocol)[method_name])
            for parameter in tuple(signature.parameters.values())[1:]:
                lowered_name = parameter.name.lower()
                assert not any(
                    fragment == lowered_name or lowered_name.endswith("_" + fragment)
                    for fragment in banned_parameter_fragments
                )
                annotation_text = repr(parameter.annotation).lower()
                assert not any(prefix in annotation_text for prefix in banned_module_prefixes)
            return_text = repr(signature.return_annotation).lower()
            assert not any(prefix in return_text for prefix in banned_module_prefixes)
