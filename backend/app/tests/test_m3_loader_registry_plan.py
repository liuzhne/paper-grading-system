"""M3 published-rubric loading, checker registry and plan-building contracts.

The target implementations deliberately do not exist when this specification
is introduced.  Every capability gate is a strict xfail which recognizes only
the exact missing module or symbol.  Import defects inside an implementation,
and every behavioral contract failure after the symbol appears, remain normal
test failures.

These tests stop at the M3 vertical slice: they freeze graph verification,
versioned checker resolution and deterministic plan construction.  General
AtomicRule execution order/effects are intentionally left to M4.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass
from datetime import timedelta
import importlib
import inspect
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.tests.m2_contract_fixtures import (
    PROFILE_KEY,
    PROFILE_VERSION,
    technical_policy_snapshot_payload,
)
from backend.app.tests.m3_contract_fixtures import (
    CHECKER_KEY,
    CHECKER_VERSION,
    DETERMINISTIC_RULE_CODE,
    ENGINE_CONTRACT_VERSION,
    PLAN_HASH_SCHEME,
    PLAN_SCHEMA_VERSION,
    POLICY_COMPILER_VERSION,
    SEMANTIC_RULE_CODE,
    checker_manifest_payload,
    checker_registration_payload,
    expected_plan_payload,
    plan_hash_projection,
    published_rubric_payload,
    published_version_content_payload,
)


LOADER_MODULE = "backend.app.services.scoring.adapters.rubric_snapshot"
REGISTRY_MODULE = "backend.app.services.scoring.core.checker_registry"
PLAN_MODULE = "backend.app.services.scoring.core.execution_plan"


class M3CapabilityUnavailable(RuntimeError):
    """The sole exception allowed to turn an M3 gate into XFAIL."""


@dataclass(frozen=True, slots=True)
class _CapabilityProbe:
    target: str
    module: ModuleType | None
    symbols: Mapping[str, object]
    missing_symbols: tuple[str, ...]
    import_error: ModuleNotFoundError | None = None

    def has(self, name: str) -> bool:
        return self.module is not None and name in self.symbols

    def require(self, name: str):
        if not self.has(name):
            error = M3CapabilityUnavailable(
                f"{self.target} must expose M3 capability {name}"
            )
            if self.import_error is not None:
                raise error from self.import_error
            raise error
        return self.symbols[name]


def _probe_module(target: str, required: tuple[str, ...]) -> _CapabilityProbe:
    try:
        module = importlib.import_module(target)
    except ModuleNotFoundError as exc:
        target_or_parent_missing = exc.name == target or (
            exc.name is not None and target.startswith(exc.name + ".")
        )
        if not target_or_parent_missing:
            raise
        return _CapabilityProbe(
            target=target,
            module=None,
            symbols={},
            missing_symbols=required,
            import_error=exc,
        )

    # Do not use getattr(): module-level dynamic lookup must not make a missing
    # API appear implemented, and AttributeError inside import must surface.
    namespace = vars(module)
    symbols = {name: namespace[name] for name in required if name in namespace}
    return _CapabilityProbe(
        target=target,
        module=module,
        symbols=symbols,
        missing_symbols=tuple(name for name in required if name not in namespace),
    )


_LOADER_PROBE = _probe_module(LOADER_MODULE, ("CompiledRubricSnapshotLoader",))
_REGISTRY_PROBE = _probe_module(REGISTRY_MODULE, ("VersionedCheckerRegistry",))
_PLAN_PROBE = _probe_module(PLAN_MODULE, ("RuleExecutionPlanBuilder",))

_loader_type = _LOADER_PROBE.symbols.get("CompiledRubricSnapshotLoader")
_LOADER_SESSION_API_READY = (
    _loader_type is not None
    and inspect.getattr_static(_loader_type, "load_from_session", None) is not None
)

# Importing the current model module is safe and lets the session integration
# gate distinguish a missing 0013 schema from a loader behavior defect.
from backend.app.db import models as _models  # noqa: E402


_DB_VERSION_IDENTITY_READY = {
    "business_profile_key",
    "hash_scheme",
}.issubset(_models.RubricVersion.__table__.c.keys())


@pytest.fixture()
def m3_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    _models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _requires(*requirements: tuple[_CapabilityProbe, str]):
    missing = [
        f"{probe.target}.{name}"
        for probe, name in requirements
        if not probe.has(name)
    ]
    return pytest.mark.xfail(
        condition=bool(missing),
        reason="M3 capability is not implemented: " + ", ".join(missing),
        raises=M3CapabilityUnavailable,
        strict=True,
    )


requires_loader = _requires(
    (_LOADER_PROBE, "CompiledRubricSnapshotLoader"),
)
requires_registry = _requires(
    (_REGISTRY_PROBE, "VersionedCheckerRegistry"),
)
requires_plan_builder = _requires(
    (_PLAN_PROBE, "RuleExecutionPlanBuilder"),
    (_REGISTRY_PROBE, "VersionedCheckerRegistry"),
)
requires_db_loader_to_plan = pytest.mark.xfail(
    condition=(
        not _LOADER_PROBE.has("CompiledRubricSnapshotLoader")
        or not _LOADER_SESSION_API_READY
        or not _REGISTRY_PROBE.has("VersionedCheckerRegistry")
        or not _PLAN_PROBE.has("RuleExecutionPlanBuilder")
        or not _DB_VERSION_IDENTITY_READY
    ),
    reason="M3 DB loader-to-plan vertical capability is not implemented",
    raises=M3CapabilityUnavailable,
    strict=True,
)


def _snapshot_mapping(value) -> dict:
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    method = getattr(value, "to_mapping", None)
    assert callable(method), "Core snapshots and plans must expose to_mapping()"
    mapped = method()
    assert isinstance(mapped, Mapping)
    return deepcopy(dict(mapped))


def _refresh_policy_hash(policy: dict) -> None:
    projection = deepcopy(policy)
    projection.pop("policy_hash", None)
    policy["policy_hash"] = canonical_sha256(projection)


def _refresh_rubric_snapshot_hash(rubric: dict) -> None:
    projection = deepcopy(rubric)
    projection.pop("rubric_snapshot_hash", None)
    projection["criteria"] = sorted(
        projection["criteria"], key=lambda item: item["criterion_code"]
    )
    projection["atomic_rules"] = sorted(
        projection["atomic_rules"], key=lambda item: item["rule_code"]
    )
    for rule in projection["atomic_rules"]:
        rule["levels"] = sorted(
            rule["levels"],
            key=lambda item: (item["display_order"], item["level_code"]),
        )
    rubric["rubric_snapshot_hash"] = canonical_sha256(
        {"scheme": "compiled-rubric-snapshot-v1", **projection}
    )


def _version_content_projection(graph: Mapping[str, object]) -> dict:
    """Independent, closed M3 content projection selected by hash_scheme.

    The projection is the full provenance graph, not the smaller executable
    snapshot. v1 deliberately excludes profile; v2 includes it in the frozen
    version subobject. ``hash_scheme`` itself is never hash content.
    """

    scheme = graph["hash_scheme"]
    content = deepcopy(graph["version_content"])
    expected_sections = {
        "rubric",
        "criteria",
        "compilation",
        "artifacts",
        "version",
        "rules",
    }
    if set(content) != expected_sections:
        raise ValueError("version_content must be the closed provenance graph")
    if scheme == "rubric-content-v1":
        if "business_profile_key" in content["version"]:
            raise ValueError("rubric-content-v1 must preserve its pre-0013 bytes")
        return content
    if scheme == "rubric-content-v2":
        if content["version"].get("business_profile_key") != graph[
            "business_profile_key"
        ]:
            raise ValueError("v2 provenance profile does not match stored version")
        return content
    raise ValueError(f"unsupported test hash scheme: {scheme}")


def _stored_version_graph(
    *,
    hash_scheme: str = "rubric-content-v2",
    profile_key: str = PROFILE_KEY,
) -> dict:
    rubric = published_rubric_payload(
        hash_scheme=hash_scheme,
        profile_key=profile_key,
    )
    graph = {
        "schema_version": "stored-rubric-version-graph@1",
        "rubric_version_id": rubric["rubric_version_id"],
        "rubric_status": "published",
        # RubricCompilation uses validated as its terminal content state;
        # published_at records the separate reviewer sign-off performed by
        # lifecycle.publish_rubric().
        "compilation_status": "validated",
        "compilation_published_at": "2026-07-19T08:30:00Z",
        "compilation_final_version_hash": rubric["version_hash"],
        "version_hash": rubric["version_hash"],
        "hash_scheme": hash_scheme,
        "business_profile_key": profile_key,
        "version_content": published_version_content_payload(
            hash_scheme=hash_scheme,
            profile_key=profile_key,
        ),
        "compiled_snapshot": rubric,
    }
    assert graph["version_hash"] == canonical_sha256(
        _version_content_projection(graph)
    )
    return graph


def _load_snapshot(graph: dict, *, expected_profile_key: str):
    loader_type = _LOADER_PROBE.require("CompiledRubricSnapshotLoader")
    loader = loader_type()
    return loader.load(
        version_graph=graph,
        expected_profile_key=expected_profile_key,
    )


def _require_db_vertical_api():
    loader_type = _LOADER_PROBE.require("CompiledRubricSnapshotLoader")
    registry_type = _REGISTRY_PROBE.require("VersionedCheckerRegistry")
    builder_type = _PLAN_PROBE.require("RuleExecutionPlanBuilder")
    if not _LOADER_SESSION_API_READY:
        raise M3CapabilityUnavailable(
            f"{LOADER_MODULE}.CompiledRubricSnapshotLoader.load_from_session is missing"
        )
    if not _DB_VERSION_IDENTITY_READY:
        raise M3CapabilityUnavailable(
            "migration 0013 RubricVersion business_profile_key/hash_scheme is missing"
        )
    return loader_type, registry_type, builder_type


def _required_fields_checker(*, document, params):
    required = tuple(params["required_fields"])
    return {
        "present": {
            field: field in document.get("profile_extensions", {}) for field in required
        }
    }


def _validate_required_fields_params(params):
    if not isinstance(params, Mapping) or set(params) != {"required_fields"}:
        raise ValueError("required-fields params must contain only required_fields")
    fields = params["required_fields"]
    if (
        not isinstance(fields, (list, tuple))
        or not fields
        or any(not isinstance(item, str) or not item.strip() for item in fields)
    ):
        raise ValueError("required_fields must be a non-empty list of names")


def _registry(
    *,
    registration: dict | None = None,
    checker=_required_fields_checker,
):
    registry_type = _REGISTRY_PROBE.require("VersionedCheckerRegistry")
    registry = registry_type()
    registry.register(
        registration=registration or checker_registration_payload(),
        checker=checker,
        validate_params=_validate_required_fields_params,
    )
    return registry


@dataclass(frozen=True, slots=True)
class _TestProfile:
    profile_key: str = PROFILE_KEY
    profile_version: str = PROFILE_VERSION
    prompt_version: str = "technical-proposal-prompt@1"

    def select_prompt_metadata(self, *, metadata):
        del metadata
        return {}

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        del submission_snapshot, document_snapshot
        return {}


def _build_plan(
    *,
    rubric: dict | None = None,
    registry=None,
    profile: _TestProfile | None = None,
    policy_compiler_version: str = POLICY_COMPILER_VERSION,
    engine_contract_version: str = ENGINE_CONTRACT_VERSION,
):
    builder_type = _PLAN_PROBE.require("RuleExecutionPlanBuilder")
    builder = builder_type(
        checker_registry=registry or _registry(),
        policy_compiler_version=policy_compiler_version,
        engine_contract_version=engine_contract_version,
    )
    return builder.build(
        rubric=rubric or published_rubric_payload(),
        profile=profile or _TestProfile(),
        document_schema_version="document-snapshot@1",
    )


# ---------------------------------------------------------------------------
# CompiledRubricSnapshotLoader


@requires_loader
def test_loader_accepts_only_a_published_validated_version_and_detaches_result():
    graph = _stored_version_graph()
    expected = deepcopy(graph["compiled_snapshot"])

    snapshot = _load_snapshot(graph, expected_profile_key=PROFILE_KEY)
    assert _snapshot_mapping(snapshot) == expected

    graph["compiled_snapshot"]["criteria"][0]["name"] = "mutated ORM value"
    assert _snapshot_mapping(snapshot) == expected
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        snapshot.business_profile_key = "changed-in-place"
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        snapshot["business_profile_key"] = "changed-in-place"
    snapshot_criteria = (
        snapshot["criteria"] if isinstance(snapshot, Mapping) else snapshot.criteria
    )
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        snapshot_criteria[0]["name"] = "changed-in-place"


@requires_loader
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("rubric_status", "review"),
        ("compilation_status", "failed"),
        ("compilation_published_at", None),
    ),
)
def test_loader_rejects_every_unpublished_or_unvalidated_state(field, value):
    graph = _stored_version_graph()
    graph[field] = value

    with pytest.raises((TypeError, ValueError), match=r"(?i)(publish|valid|status)"):
        _load_snapshot(graph, expected_profile_key=PROFILE_KEY)


@requires_loader
@pytest.mark.parametrize(
    "tamper",
    (
        "stored-version-vs-compilation",
        "snapshot-version-vs-row",
        "snapshot-version-id-vs-row",
        "snapshot-scheme-vs-row",
        "snapshot-profile-vs-row",
        "recomputed-full-graph",
        "compiled-vs-version-graph",
        "snapshot-content-hash",
    ),
)
def test_loader_recomputes_declared_hash_and_rejects_all_hash_disagreement(tamper):
    graph = _stored_version_graph()
    if tamper == "stored-version-vs-compilation":
        graph["compilation_final_version_hash"] = "0" * 64
    elif tamper == "snapshot-version-vs-row":
        graph["compiled_snapshot"]["version_hash"] = "0" * 64
        _refresh_rubric_snapshot_hash(graph["compiled_snapshot"])
    elif tamper == "snapshot-version-id-vs-row":
        graph["compiled_snapshot"]["rubric_version_id"] += "-other"
        _refresh_rubric_snapshot_hash(graph["compiled_snapshot"])
    elif tamper == "snapshot-scheme-vs-row":
        graph["compiled_snapshot"]["hash_scheme"] = "rubric-content-v1"
        _refresh_rubric_snapshot_hash(graph["compiled_snapshot"])
    elif tamper == "snapshot-profile-vs-row":
        graph["compiled_snapshot"]["business_profile_key"] = "thesis"
        _refresh_rubric_snapshot_hash(graph["compiled_snapshot"])
    elif tamper == "recomputed-full-graph":
        # The two stored strings still agree.  A loader which merely compares
        # them will incorrectly accept this provenance behavior change.
        graph["version_content"]["compilation"]["parser_version"] += "+tampered"
    elif tamper == "compiled-vs-version-graph":
        # Each supplied hash remains internally valid, but the executable
        # projection no longer represents the authoritative provenance graph.
        graph["compiled_snapshot"]["criteria"][0]["name"] = "tampered"
        _refresh_rubric_snapshot_hash(graph["compiled_snapshot"])
    else:
        graph["compiled_snapshot"]["rubric_snapshot_hash"] = "0" * 64

    with pytest.raises(
        (TypeError, ValueError),
        match=r"(?i)(hash|content|compilation|version|profile|scheme|identity|graph)",
    ):
        _load_snapshot(graph, expected_profile_key=PROFILE_KEY)


@requires_loader
def test_loader_uses_declared_hash_scheme_and_rejects_an_unknown_scheme():
    graph = _stored_version_graph()
    graph["hash_scheme"] = "rubric-content-future"
    graph["compiled_snapshot"]["hash_scheme"] = "rubric-content-future"

    with pytest.raises((TypeError, ValueError), match=r"(?i)(scheme|unsupported)"):
        _load_snapshot(graph, expected_profile_key=PROFILE_KEY)


@requires_loader
def test_loader_mapping_boundary_rejects_unprojected_orm_or_adapter_fields():
    graph = _stored_version_graph()
    graph["lazy_relationship"] = {"database_id": "mutable-row"}

    with pytest.raises((TypeError, ValueError), match=r"(?i)(unknown|field|graph)"):
        _load_snapshot(graph, expected_profile_key=PROFILE_KEY)


@requires_loader
def test_loader_accepts_backfilled_thesis_v1_without_folding_profile_into_v1_hash():
    graph = _stored_version_graph(
        hash_scheme="rubric-content-v1",
        profile_key="thesis",
    )
    baseline_hash = graph["version_hash"]
    projection = _version_content_projection(graph)

    assert "business_profile_key" not in projection["version"]
    snapshot = _load_snapshot(graph, expected_profile_key="thesis")
    assert _snapshot_mapping(snapshot)["version_hash"] == baseline_hash


@requires_loader
def test_loader_rejects_non_thesis_v1_and_profile_expectation_mismatch():
    non_thesis_v1 = _stored_version_graph(
        hash_scheme="rubric-content-v1",
        profile_key=PROFILE_KEY,
    )
    with pytest.raises((TypeError, ValueError), match=r"(?i)(profile|v1|thesis)"):
        _load_snapshot(non_thesis_v1, expected_profile_key=PROFILE_KEY)

    v2 = _stored_version_graph()
    with pytest.raises((TypeError, ValueError), match=r"(?i)profile"):
        _load_snapshot(v2, expected_profile_key="thesis")


@requires_loader
def test_loader_v2_hash_binds_business_profile_key():
    graph = _stored_version_graph()
    graph["business_profile_key"] = "thesis"
    graph["compiled_snapshot"]["business_profile_key"] = "thesis"
    _refresh_rubric_snapshot_hash(graph["compiled_snapshot"])

    with pytest.raises((TypeError, ValueError), match=r"(?i)(hash|profile)"):
        _load_snapshot(graph, expected_profile_key="thesis")


# ---------------------------------------------------------------------------
# VersionedCheckerRegistry


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _checker_package_projection(registration: Mapping[str, object]) -> dict:
    return {
        "scheme": "checker-package-sha256-v1",
        "checker_key": registration["checker_key"],
        "checker_version": registration["checker_version"],
        "entrypoint": registration["entrypoint"],
        "runtime_contract_version": registration["runtime_contract_version"],
        "dependency_manifest_hash": registration["dependency_manifest_hash"],
        "artifact_manifest": registration["artifact_manifest"],
    }


def _refresh_checker_implementation_hash(registration: dict) -> None:
    registration["implementation_hash"] = canonical_sha256(
        _checker_package_projection(registration)
    )


def _resolve_checker(registry, *, params=None, profile_key=PROFILE_KEY, schema=None):
    return registry.resolve(
        checker_key=CHECKER_KEY,
        checker_version=CHECKER_VERSION,
        checker_params=(
            {"required_fields": ["owner"]} if params is None else params
        ),
        profile_key=profile_key,
        document_schema_version=schema or "document-snapshot@1",
    )


@requires_registry
def test_registry_resolves_exact_immutable_key_and_version_and_freezes_manifest():
    registration = checker_registration_payload()
    registry = _registry(registration=registration)

    assert _resolve_checker(registry) is _required_fields_checker
    manifest = registry.manifest(
        profile_key=PROFILE_KEY,
        document_schema_version="document-snapshot@1",
    )
    manifest_mapping = _thaw(manifest)
    assert manifest_mapping == {
        CHECKER_KEY: {
            key: deepcopy(registration[key])
            for key in (
                "checker_version",
                "implementation_hash",
                "params_schema",
                "supported_document_schemas",
                "supported_profiles",
                "observation_schema",
            )
        }
    }

    registration["artifact_manifest"][0]["sha256"] = "0" * 64
    registration["supported_profiles"].append("thesis")
    assert _thaw(manifest) == manifest_mapping
    with pytest.raises((AttributeError, TypeError)):
        manifest[CHECKER_KEY]["checker_version"] = "changed"
    with pytest.raises((AttributeError, TypeError)):
        manifest[CHECKER_KEY]["supported_profiles"].append("thesis")
    assert _thaw(
        registry.manifest(
            profile_key=PROFILE_KEY,
            document_schema_version="document-snapshot@1",
        )
    ) == manifest_mapping


@requires_registry
def test_registry_rejects_a_second_version_for_the_same_versioned_checker_key():
    registry = _registry()
    second_registration = checker_registration_payload(checker_version="1.1.0")

    with pytest.raises(
        (TypeError, ValueError),
        match=r"(?i)(ambiguous|checker|duplicate|immutable|key|version)",
    ):
        registry.register(
            registration=second_registration,
            checker=lambda **_: {"implementation": "1.1.0"},
            validate_params=_validate_required_fields_params,
        )

    assert _resolve_checker(registry) is _required_fields_checker


@requires_registry
def test_registry_supports_a_new_immutable_version_under_an_explicit_new_key():
    registry = _registry()
    second_registration = checker_registration_payload(checker_version="2.0.0")
    second_registration["checker_key"] = "technical_proposal.required_fields.v2"
    _refresh_checker_implementation_hash(second_registration)

    def second_checker(*, document, params):
        del document, params
        return {"implementation": "2.0.0"}

    registry.register(
        registration=second_registration,
        checker=second_checker,
        validate_params=_validate_required_fields_params,
    )

    assert _resolve_checker(registry) is _required_fields_checker
    assert registry.resolve(
        checker_key="technical_proposal.required_fields.v2",
        checker_version="2.0.0",
        checker_params={"required_fields": ["owner"]},
        profile_key=PROFILE_KEY,
        document_schema_version="document-snapshot@1",
    ) is second_checker


@requires_registry
def test_registry_uses_explicit_callable_and_never_dynamic_imports_entrypoint(
    monkeypatch,
):
    def unexpected_dynamic_import(*_args, **_kwargs):
        raise AssertionError("CheckerRegistry must not dynamically import entrypoints")

    monkeypatch.setattr(importlib, "import_module", unexpected_dynamic_import)
    registry = _registry()

    assert _resolve_checker(registry) is _required_fields_checker


@requires_registry
def test_registry_recomputes_checker_package_hash_instead_of_trusting_manifest_string():
    registration = checker_registration_payload()
    registration["artifact_manifest"][0]["sha256"] = "0" * 64
    # Deliberately retain the old implementation_hash.

    with pytest.raises((TypeError, ValueError), match=r"(?i)(implementation|hash)"):
        _registry(registration=registration)


@requires_registry
@pytest.mark.parametrize(
    "invalid_registration",
    (
        {"schema_version": "checker-registration@2"},
        {"checker_key": "technical_proposal.required_fields"},
        {"checker_version": ""},
        {"entrypoint": ""},
        {"runtime_contract_version": ""},
        {"dependency_manifest_hash": "not-a-sha256"},
        {"artifact_manifest": []},
        {"artifact_manifest": [{"path": "/tmp/checker.py", "sha256": "1" * 64}]},
        {"artifact_manifest": [{"path": "checker.py", "sha256": "not-a-sha256"}]},
        {"supported_profiles": []},
        {"supported_document_schemas": []},
        {"params_schema": ""},
        {"observation_schema": ""},
        {"unexpected_runtime_loader": "forbidden"},
    ),
)
def test_registry_rejects_unversioned_or_incomplete_registration_contract(
    invalid_registration,
):
    registration = checker_registration_payload()
    registration.update(invalid_registration)
    if not invalid_registration.keys() & {
        "checker_key",
        "checker_version",
        "entrypoint",
        "runtime_contract_version",
        "dependency_manifest_hash",
        "artifact_manifest",
    }:
        # Non-package manifest fields are outside implementation_hash.
        pass
    else:
        _refresh_checker_implementation_hash(registration)

    with pytest.raises(
        (TypeError, ValueError),
        match=r"(?i)(checker|version|schema|profile|entrypoint|runtime|dependency|artifact|unknown)",
    ):
        _registry(registration=registration)


@requires_registry
def test_registry_never_replaces_an_existing_key_version_in_place():
    registry = _registry()
    changed = checker_registration_payload()
    changed["artifact_manifest"][0]["sha256"] = "e" * 64
    _refresh_checker_implementation_hash(changed)

    with pytest.raises((TypeError, ValueError), match=r"(?i)(duplicate|immutable|exist|replace)"):
        registry.register(
            registration=changed,
            checker=lambda **_: {"changed": True},
            validate_params=_validate_required_fields_params,
        )
    assert _resolve_checker(registry) is _required_fields_checker


@requires_registry
@pytest.mark.parametrize(
    ("checker_key", "checker_version"),
    (
        ("technical_proposal.unknown.v1", CHECKER_VERSION),
        (CHECKER_KEY, "9.9.9"),
    ),
)
def test_registry_unknown_key_or_version_fails_closed(checker_key, checker_version):
    registry = _registry()

    with pytest.raises((KeyError, TypeError, ValueError)):
        registry.resolve(
            checker_key=checker_key,
            checker_version=checker_version,
            checker_params={"required_fields": ["owner"]},
            profile_key=PROFILE_KEY,
            document_schema_version="document-snapshot@1",
        )


@requires_registry
@pytest.mark.parametrize(
    ("profile_key", "document_schema_version"),
    (
        ("thesis", "document-snapshot@1"),
        (PROFILE_KEY, "document-snapshot@2"),
    ),
)
def test_registry_blocks_unsupported_profile_or_document_schema(
    profile_key,
    document_schema_version,
):
    registry = _registry()

    with pytest.raises((TypeError, ValueError), match=r"(?i)(profile|document|schema|support)"):
        registry.resolve(
            checker_key=CHECKER_KEY,
            checker_version=CHECKER_VERSION,
            checker_params={"required_fields": ["owner"]},
            profile_key=profile_key,
            document_schema_version=document_schema_version,
        )


@requires_registry
@pytest.mark.parametrize(
    "params",
    (
        {},
        {"required_fields": []},
        {"required_fields": ["owner"], "unrecognized": True},
    ),
)
def test_registry_validates_checker_params_before_runtime(params):
    registry = _registry()

    with pytest.raises((TypeError, ValueError), match=r"(?i)(param|required_fields)"):
        _resolve_checker(registry, params=params)


@requires_registry
def test_registry_rejects_a_non_callable_checker_at_registration_time():
    with pytest.raises((TypeError, ValueError), match=r"(?i)(callable|checker)"):
        _registry(checker={"not": "callable"})


# ---------------------------------------------------------------------------
# RuleExecutionPlanBuilder


@requires_plan_builder
def test_builder_compiles_published_fixture_to_complete_stable_plan_v2():
    plan = _build_plan()
    mapping = _snapshot_mapping(plan)
    expected = expected_plan_payload()

    assert mapping == expected
    assert mapping["schema_version"] == PLAN_SCHEMA_VERSION
    assert mapping["hash_scheme"] == PLAN_HASH_SCHEME
    assert mapping["plan_hash"] == canonical_sha256(plan_hash_projection(mapping))
    assert mapping["dependency_order"] == sorted(mapping["dependency_order"])
    assert [node["rule_code"] for node in mapping["nodes"]] == mapping[
        "dependency_order"
    ]
    assert all(
        {"criterion_snapshot", "atomic_rule_snapshot"}.issubset(node)
        for node in mapping["nodes"]
    )


@requires_plan_builder
def test_builder_stably_sorts_criteria_rules_levels_and_mapping_keys():
    rubric = published_rubric_payload()
    rubric["criteria"].reverse()
    rubric["atomic_rules"].reverse()
    for rule in rubric["atomic_rules"]:
        rule["checker_params"] = dict(reversed(tuple(rule["checker_params"].items())))
        rule["levels"].reverse()
    rubric = dict(reversed(tuple(rubric.items())))

    reordered = _snapshot_mapping(_build_plan(rubric=rubric))
    baseline = _snapshot_mapping(_build_plan())
    assert reordered == baseline
    assert reordered["plan_hash"] == baseline["plan_hash"]


@requires_plan_builder
def test_builder_plan_contains_only_the_exact_checker_versions_rules_consume():
    registry = _registry()
    unused = checker_registration_payload(checker_version="2.0.0")
    unused["checker_key"] = "technical_proposal.unused.v2"
    _refresh_checker_implementation_hash(unused)
    registry.register(
        registration=unused,
        checker=lambda **_: {"unused": True},
        validate_params=_validate_required_fields_params,
    )

    plan = _snapshot_mapping(_build_plan(registry=registry))
    assert set(plan["checker_manifest"]) == {CHECKER_KEY}
    assert plan["plan_hash"] == expected_plan_payload()["plan_hash"]


def _changed_registry():
    registration = checker_registration_payload()
    registration["artifact_manifest"][0]["sha256"] = "e" * 64
    _refresh_checker_implementation_hash(registration)
    return _registry(registration=registration)


@requires_plan_builder
@pytest.mark.parametrize(
    "identity_part",
    (
        "rubric",
        "policy",
        "business-profile-version",
        "checker-manifest",
        "policy-compiler",
        "engine-contract",
    ),
)
def test_plan_hash_changes_when_any_complete_identity_input_changes(identity_part):
    rubric = published_rubric_payload()
    registry = None
    profile = _TestProfile()
    policy_compiler_version = POLICY_COMPILER_VERSION
    engine_contract_version = ENGINE_CONTRACT_VERSION
    baseline = _snapshot_mapping(_build_plan())

    if identity_part == "rubric":
        rubric["criteria"][0]["name"] = "Changed risk-control wording"
        rubric["version_hash"] = "e" * 64
        _refresh_rubric_snapshot_hash(rubric)
    elif identity_part == "policy":
        rubric["global_policy"]["rounding"]["digits"] = 1
        _refresh_policy_hash(rubric["global_policy"])
        _refresh_rubric_snapshot_hash(rubric)
    elif identity_part == "business-profile-version":
        profile = _TestProfile(profile_version="technical-proposal-test-profile@2")
    elif identity_part == "checker-manifest":
        registry = _changed_registry()
    elif identity_part == "policy-compiler":
        policy_compiler_version = "scoring-policy-compiler@2"
    else:
        engine_contract_version = "scoring-core@2"

    changed = _snapshot_mapping(
        _build_plan(
            rubric=rubric,
            registry=registry,
            profile=profile,
            policy_compiler_version=policy_compiler_version,
            engine_contract_version=engine_contract_version,
        )
    )
    assert changed["plan_hash"] != baseline["plan_hash"]
    assert changed["plan_hash"] == canonical_sha256(plan_hash_projection(changed))
    if identity_part == "rubric":
        assert changed["checker_manifest"] == baseline["checker_manifest"]


@requires_plan_builder
@pytest.mark.parametrize(
    "invalid",
    (
        "stale-rubric-snapshot-hash",
        "unpublished-source-kind",
        "profile-mismatch",
        "unknown-checker",
        "invalid-checker-params",
        "policy-hash-mismatch",
    ),
)
def test_builder_fails_closed_before_plan_creation_for_invalid_inputs(invalid):
    rubric = published_rubric_payload()
    profile = _TestProfile()
    if invalid == "stale-rubric-snapshot-hash":
        rubric["criteria"][0]["name"] = "tampered without rehash"
    elif invalid == "unpublished-source-kind":
        rubric["rubric_source_kind"] = "review_candidate"
        _refresh_rubric_snapshot_hash(rubric)
    elif invalid == "profile-mismatch":
        profile = _TestProfile(profile_key="thesis")
    elif invalid == "unknown-checker":
        rubric["atomic_rules"][0]["checker_key"] = "technical_proposal.unknown.v1"
        _refresh_rubric_snapshot_hash(rubric)
    elif invalid == "invalid-checker-params":
        rubric["atomic_rules"][0]["checker_params"] = {"required_fields": []}
        _refresh_rubric_snapshot_hash(rubric)
    else:
        rubric["global_policy"]["policy_hash"] = "0" * 64
        _refresh_rubric_snapshot_hash(rubric)

    with pytest.raises((KeyError, TypeError, ValueError)):
        _build_plan(rubric=rubric, profile=profile)


@requires_plan_builder
def test_builder_validates_document_schema_against_checker_before_plan_hashing():
    builder_type = _PLAN_PROBE.require("RuleExecutionPlanBuilder")
    builder = builder_type(
        checker_registry=_registry(),
        policy_compiler_version=POLICY_COMPILER_VERSION,
        engine_contract_version=ENGINE_CONTRACT_VERSION,
    )

    with pytest.raises((TypeError, ValueError), match=r"(?i)(document|schema|support)"):
        builder.build(
            rubric=published_rubric_payload(),
            profile=_TestProfile(),
            document_schema_version="document-snapshot@2",
        )


@requires_plan_builder
def test_built_plan_is_detached_and_recursively_immutable():
    rubric = published_rubric_payload()
    plan = _build_plan(rubric=rubric)
    before = _snapshot_mapping(plan)

    rubric["criteria"][0]["name"] = "mutated after build"
    assert _snapshot_mapping(plan) == before
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        plan.plan_hash = "0" * 64
    nodes = plan.nodes
    with pytest.raises((AttributeError, TypeError)):
        nodes.append("new node")
    with pytest.raises((AttributeError, TypeError)):
        nodes[0]["atomic_rule_snapshot"]["rule_code"] = "changed"


@requires_db_loader_to_plan
def test_published_database_fixture_remains_complete_after_session_close_and_builds_plan(
    m3_db,
):
    """M3 completion gate: published DB graph -> detached snapshot -> plan.

    This test intentionally closes the SQLAlchemy Session between loading and
    plan construction.  A loader that leaves lazy ORM relationships inside the
    snapshot therefore fails with a real DetachedInstanceError instead of
    receiving an expected-failure exemption.
    """

    loader_type, _registry_type, builder_type = _require_db_vertical_api()
    db = m3_db
    from backend.app.services.rubrics import lifecycle
    from backend.app.tests.test_atomic_rule_models import _make_p1_graph

    graph = _make_p1_graph(db, "m3-db-loader-plan")
    graph.rubric.total_score = 100
    graph.criterion.code = "SOLUTION_FIT"
    graph.criterion.name = "Solution fit"
    graph.criterion.max_score = 80
    graph.criterion.criterion_type = "llm_judgment"
    graph.criterion.scoring_mode = "banded"
    graph.criterion.applies_to = "requirements_understanding"
    graph.criterion.display_order = 1
    risk_criterion = _models.RubricCriterion(
        rubric_id=graph.rubric.id,
        code="RISK_CONTROL",
        name="Risk control completeness",
        max_score=20,
        criterion_type="deterministic",
        scoring_mode="deductive",
        applies_to="risk_control",
        dimension="content",
        display_order=0,
    )
    db.add(risk_criterion)
    db.flush()
    graph.version.business_profile_key = PROFILE_KEY
    graph.version.hash_scheme = "rubric-content-v2"
    graph.version.global_policy = technical_policy_snapshot_payload(
        total_score="100",
        rounding_digits=2,
    )

    reviewed_at = graph.compilation.created_at + timedelta(minutes=1)
    deterministic_rule = _models.AtomicRule(
        rubric_version_id=graph.version.id,
        criterion_id=risk_criterion.id,
        rule_code=DETERMINISTIC_RULE_CODE,
        name="Risk owner required",
        rule_text="Risk control must identify an accountable owner.",
        direction="deduct",
        effect_type="score",
        max_points=10,
        repeat_policy="once",
        cap_points=None,
        judge_type="deterministic",
        checker_key=CHECKER_KEY,
        checker_params={"required_fields": ["owner"]},
        evidence_policy={
            "mode": "scoped_absence",
            "requirement": "required",
            "minimum_coverage": "1",
        },
        positive_example="Each risk names an owner.",
        negative_example="No accountable owner is named.",
        boundary_example=None,
        strictness="required",
        applies_to="risk_control",
        mutex_group=None,
        depends_on_rule_codes=[],
        status="approved",
        creation_method="compiler",
        reviewed_by=graph.user.id,
        reviewed_at=reviewed_at,
    )
    semantic_rule = _models.AtomicRule(
        rubric_version_id=graph.version.id,
        criterion_id=graph.criterion.id,
        rule_code=SEMANTIC_RULE_CODE,
        name="Solution fit",
        rule_text="Requirements and solution must be explicitly aligned.",
        direction="band",
        effect_type="score",
        max_points=None,
        repeat_policy=None,
        cap_points=None,
        judge_type="semantic",
        checker_key=None,
        checker_params={},
        evidence_policy={
            "mode": "source_quote",
            "requirement": "required",
            "minimum_coverage": "1",
        },
        positive_example="The mapping is explicit.",
        negative_example="Requirements are only listed.",
        boundary_example=None,
        strictness="required",
        applies_to="requirements_understanding",
        mutex_group=None,
        depends_on_rule_codes=[],
        status="approved",
        creation_method="compiler",
        reviewed_by=graph.user.id,
        reviewed_at=reviewed_at,
    )
    db.add_all([deterministic_rule, semantic_rule])
    db.flush()
    db.add_all(
        [
            _models.RuleLevel(
                atomic_rule_id=semantic_rule.id,
                level_code="FIT_LOW",
                points=40,
                descriptor="The solution only partially addresses requirements.",
                positive_example=None,
                negative_example=None,
                display_order=1,
            ),
            _models.RuleLevel(
                atomic_rule_id=semantic_rule.id,
                level_code="FIT_HIGH",
                points=80,
                descriptor="Requirements and solution are explicitly aligned.",
                positive_example=None,
                negative_example=None,
                display_order=0,
            ),
        ]
    )
    db.commit()

    lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()
    published = lifecycle.publish_rubric(
        db,
        graph.rubric.id,
        graph.compilation.id,
        graph.user.id,
        now=reviewed_at + timedelta(minutes=5),
    )
    db.commit()
    published_id = published.id

    snapshot = loader_type().load_from_session(
        session=db,
        rubric_version_id=published_id,
        expected_profile_key=PROFILE_KEY,
    )
    before_close = _snapshot_mapping(snapshot)
    db.expunge_all()
    db.close()

    assert _snapshot_mapping(snapshot) == before_close
    assert before_close["rubric_source_kind"] == "published_version"
    assert before_close["rubric_version_id"] == published_id
    assert before_close["business_profile_key"] == PROFILE_KEY
    assert before_close["hash_scheme"] == "rubric-content-v2"
    assert before_close["version_hash"]
    rules = {item["rule_code"]: item for item in before_close["atomic_rules"]}
    assert set(rules) == {DETERMINISTIC_RULE_CODE, SEMANTIC_RULE_CODE}
    assert rules[SEMANTIC_RULE_CODE]["levels"][0]["level_code"] == "FIT_HIGH"
    assert rules[DETERMINISTIC_RULE_CODE]["checker_key"] == CHECKER_KEY
    assert rules[DETERMINISTIC_RULE_CODE]["checker_version"] is None

    plan = builder_type(
        checker_registry=_registry(),
        policy_compiler_version=POLICY_COMPILER_VERSION,
        engine_contract_version=ENGINE_CONTRACT_VERSION,
    ).build(
        rubric=snapshot,
        profile=_TestProfile(),
        document_schema_version="document-snapshot@1",
    )
    plan_mapping = _snapshot_mapping(plan)
    assert plan_mapping["rubric_snapshot_hash"] == before_close[
        "rubric_snapshot_hash"
    ]
    assert plan_mapping["rubric_version_id"] == published_id
    assert plan_mapping["rubric_version_hash"] == before_close["version_hash"]
    assert plan_mapping["rubric_hash_scheme"] == "rubric-content-v2"
    assert plan_mapping["dependency_order"] == sorted(
        [DETERMINISTIC_RULE_CODE, SEMANTIC_RULE_CODE]
    )
    plan_nodes = {item["rule_code"]: item for item in plan_mapping["nodes"]}
    assert plan_nodes[DETERMINISTIC_RULE_CODE]["atomic_rule_snapshot"][
        "checker_version"
    ] == CHECKER_VERSION
    assert plan_mapping["checker_manifest"] == checker_manifest_payload()
    assert plan_mapping["plan_hash"] == canonical_sha256(
        plan_hash_projection(plan_mapping)
    )
