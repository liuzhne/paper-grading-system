"""M2 business-profile contract exercised by an in-test technical proposal.

The TestProfile owns only business interpretation: metadata opt-in and prompt
extensions.  A small test-side Core oracle binds immutable snapshot, policy,
and registry identities around those extensions.  This keeps Profile code from
omitting or rewriting identities that belong to Core while exercising the M2
isolation boundary without a database, filesystem, provider, or M3 executor.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, is_dataclass, replace
from decimal import Decimal
import hashlib
import importlib
import inspect
import json
from types import MappingProxyType, ModuleType

import pytest

from backend.app.tests.m1_contract_helpers import prompt_envelope_payload
from backend.app.tests.m2_contract_fixtures import (
    checker_manifest_payload,
    technical_document_payload,
    technical_policy_snapshot_payload,
    technical_submission_payload,
)


PROFILE_MODULE = "backend.app.profiles.base"
POLICY_MODULE = "backend.app.services.scoring.core.policy"
CONTRACTS_MODULE = "backend.app.services.scoring.core.contracts"
PROFILE_SYMBOLS = ("BusinessProfile",)
POLICY_SYMBOLS = ("ScoringPolicy", "compile_scoring_policy")
SNAPSHOT_SYMBOLS = ("SubmissionSnapshot", "DocumentSnapshot")
CONTRACT_SYMBOLS = (*SNAPSHOT_SYMBOLS, "PromptEnvelopeV2")
M1_PROMPT_VERSION = "2026-07-19-4"


class M2CapabilityUnavailable(RuntimeError):
    """The only exception allowed to satisfy an M2 capability xfail."""


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
            error = M2CapabilityUnavailable(
                f"{self.target} must expose M2 capability {name}"
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

    namespace = vars(module)
    symbols = {name: namespace[name] for name in required if name in namespace}
    return _CapabilityProbe(
        target=target,
        module=module,
        symbols=symbols,
        missing_symbols=tuple(name for name in required if name not in namespace),
    )


_PROFILE_PROBE = _probe_module(PROFILE_MODULE, PROFILE_SYMBOLS)
_POLICY_PROBE = _probe_module(POLICY_MODULE, POLICY_SYMBOLS)
_CONTRACT_PROBE = _probe_module(CONTRACTS_MODULE, CONTRACT_SYMBOLS)


def _requires(probe: _CapabilityProbe, *names: str):
    return pytest.mark.xfail(
        condition=any(not probe.has(name) for name in names),
        reason=f"M2 capability is missing from {probe.target}: {', '.join(names)}",
        raises=M2CapabilityUnavailable,
        strict=True,
    )


requires_business_profile = _requires(_PROFILE_PROBE, "BusinessProfile")
requires_profile_snapshot_wiring = pytest.mark.xfail(
    condition=(
        not _PROFILE_PROBE.has("BusinessProfile")
        or any(not _CONTRACT_PROBE.has(name) for name in SNAPSHOT_SYMBOLS)
    ),
    reason="M2 BusinessProfile and Snapshot DTO wiring is not implemented",
    raises=M2CapabilityUnavailable,
    strict=True,
)
requires_prompt_envelope_v2 = _requires(_CONTRACT_PROBE, "PromptEnvelopeV2")


@pytest.fixture()
def business_profile_contract():
    return _PROFILE_PROBE.require("BusinessProfile")


@pytest.fixture()
def policy_contract_api():
    return {
        "ScoringPolicy": _POLICY_PROBE.require("ScoringPolicy"),
        "compile_scoring_policy": _POLICY_PROBE.require("compile_scoring_policy"),
    }


def _deep_freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [_thaw(item) for item in value]
    return value


def _canonical_hash(value) -> str:
    encoded = json.dumps(
        _thaw(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _FrozenPolicyIdentity:
    schema_version: str
    policy_hash: str


def _snapshot_mapping(value) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    method = getattr(value, "to_mapping", None)
    if callable(method):
        mapped = method()
        if isinstance(mapped, Mapping):
            return mapped
    raise TypeError("Profile requires an immutable Snapshot DTO or detached mapping")


def _technical_policy_payload(*, rounding_digits: int) -> dict:
    return technical_policy_snapshot_payload(rounding_digits=rounding_digits)


def _probe_generic_policy_compiler():
    if any(not _POLICY_PROBE.has(name) for name in POLICY_SYMBOLS):
        return M2CapabilityUnavailable(
            f"{POLICY_MODULE} does not expose the M1 ScoringPolicy compiler"
        )
    compiler = _POLICY_PROBE.require("compile_scoring_policy")
    try:
        compiler(
            _technical_policy_payload(rounding_digits=2),
            total_score=Decimal("100"),
        )
    except ValueError as exc:
        if str(exc) == "unsupported policy_key: technical_proposal_contract_policy":
            return M2CapabilityUnavailable(
                "M2 policy compiler does not support a generic business-profile key"
            )
        raise
    return None


_GENERIC_POLICY_ERROR = _probe_generic_policy_compiler()
requires_generic_policy = pytest.mark.xfail(
    condition=_GENERIC_POLICY_ERROR is not None,
    reason="M2 generic business-profile ScoringPolicy compilation is not implemented",
    raises=M2CapabilityUnavailable,
    strict=True,
)


def _require_generic_policy_compiler() -> None:
    if _GENERIC_POLICY_ERROR is not None:
        raise M2CapabilityUnavailable(str(_GENERIC_POLICY_ERROR))


def _compile_technical_policy(api, *, rounding_digits: int = 2):
    _require_generic_policy_compiler()
    compiler = api["compile_scoring_policy"]
    compiled = compiler(
        _technical_policy_payload(rounding_digits=rounding_digits),
        total_score=Decimal("100"),
    )
    assert isinstance(compiled, api["ScoringPolicy"])
    assert compiled.policy_key == "technical_proposal_contract_policy"
    assert compiled.usage == "authoritative_new_runs"
    replayed = compiler(
        compiled.to_mapping(),
        total_score=Decimal("100"),
    )
    assert replayed.to_mapping() == compiled.to_mapping()
    return compiled


def _checker_manifest(*, version: str = "1.0.0"):
    return _deep_freeze(checker_manifest_payload(version=version))


_THESIS_ONLY_FIELDS = frozenset(
    {"student_id", "student_name", "advisor", "thesis_title"}
)


@dataclass(frozen=True, slots=True)
class _TechnicalProposalTestProfile:
    profile_key: str
    profile_version: str
    prompt_version: str
    prompt_metadata_allowlist: frozenset[str]
    section_semantic_map: Mapping[str, str]

    def __post_init__(self):
        if self.profile_key != "technical_proposal":
            raise ValueError("the M2 TestProfile only supports technical_proposal")
        if self.prompt_metadata_allowlist & _THESIS_ONLY_FIELDS:
            raise ValueError("prompt metadata allowlist contains a thesis-only field")
        object.__setattr__(
            self,
            "section_semantic_map",
            _deep_freeze(self.section_semantic_map),
        )

    def select_prompt_metadata(self, *, metadata: Mapping[str, object]):
        return _deep_freeze(
            {
                key: deepcopy(metadata[key])
                for key in sorted(self.prompt_metadata_allowlist)
                if key in metadata
            }
        )

    def build_prompt_extensions(
        self,
        *,
        submission_snapshot: Mapping[str, object],
        document_snapshot: Mapping[str, object],
    ):
        submission_snapshot = _snapshot_mapping(submission_snapshot)
        document_snapshot = _snapshot_mapping(document_snapshot)
        if submission_snapshot.get("schema_version") != "submission-snapshot@1":
            raise ValueError("unsupported SubmissionSnapshot schema")
        if document_snapshot.get("schema_version") != "document-snapshot@1":
            raise ValueError("unsupported DocumentSnapshot schema")
        if submission_snapshot.get("profile_key") != self.profile_key:
            raise ValueError("submission snapshot profile does not match TestProfile")
        if document_snapshot.get("profile_key") != self.profile_key:
            raise ValueError("document snapshot profile does not match TestProfile")
        if document_snapshot.get("profile_version") != self.profile_version:
            raise ValueError("document snapshot profile version does not match TestProfile")

        section_semantics = []
        for section in document_snapshot["sections"]:
            heading = section["heading"]
            semantic_key = self.section_semantic_map.get(heading)
            if semantic_key is None:
                continue
            section_semantics.append(
                {
                    "semantic_key": semantic_key,
                    "section_path": section["section_path"],
                    "section_ordinal": section["section_ordinal"],
                    "evidence_unit_ids": section["evidence_unit_ids"],
                }
            )

        profile_extensions = document_snapshot.get("profile_extensions", {}).get(
            self.profile_key,
            {},
        )
        return _deep_freeze(
            {
                "metadata": self.select_prompt_metadata(
                    metadata=submission_snapshot.get("metadata", {})
                ),
                "section_semantics": section_semantics,
                "profile_extensions": profile_extensions,
            }
        )


def _submission_snapshot(*, metadata: Mapping[str, object] | None = None):
    return technical_submission_payload(metadata=metadata)


def _document_snapshot(*, profile_version: str = "technical-proposal-test-profile@1"):
    return technical_document_payload(profile_version=profile_version)


def _assemble_core_prompt_projection(
    *,
    profile: _TechnicalProposalTestProfile,
    policy,
    checker_manifest: Mapping[str, object],
    submission_snapshot: Mapping[str, object],
    document_snapshot: Mapping[str, object],
):
    """Test oracle for the Core-owned identity wrapper around Profile output."""

    extensions = profile.build_prompt_extensions(
        submission_snapshot=submission_snapshot,
        document_snapshot=document_snapshot,
    )
    return _deep_freeze(
        {
            "profile": {
                "key": profile.profile_key,
                "version": profile.profile_version,
                "prompt_version": profile.prompt_version,
            },
            "submission": {
                "source_artifact_hash": submission_snapshot["source_artifact_hash"],
            },
            "document": {
                "schema_version": document_snapshot["schema_version"],
                "profile_version": document_snapshot["profile_version"],
                "parser_version": document_snapshot["parser_version"],
                "normalizer_version": document_snapshot["normalizer_version"],
                "normalized_content_hash": document_snapshot["content_hash"],
                "document_snapshot_hash": document_snapshot["document_snapshot_hash"],
            },
            "policy": {
                "schema_version": policy.schema_version,
                "policy_hash": policy.policy_hash,
            },
            "checker_manifest": checker_manifest,
            "profile_prompt_extensions": extensions,
        }
    )


@pytest.fixture()
def technical_proposal_profile():
    return _TechnicalProposalTestProfile(
        profile_key="technical_proposal",
        profile_version="technical-proposal-test-profile@1",
        prompt_version="technical-proposal-prompt@1",
        prompt_metadata_allowlist=frozenset(),
        section_semantic_map={
            "需求理解": "requirements_understanding",
            "风险控制": "risk_control",
        },
    )


@pytest.fixture()
def technical_policy(policy_contract_api):
    return _compile_technical_policy(policy_contract_api)


@pytest.fixture()
def frozen_policy_identity():
    payload = {
        "schema_version": "scoring-policy@1",
        "policy_key": "technical_proposal_contract_policy",
        "usage": "authoritative_new_runs",
    }
    return _FrozenPolicyIdentity(
        schema_version=payload["schema_version"],
        policy_hash=_canonical_hash(payload),
    )


@requires_business_profile
def test_business_profile_is_a_minimal_prompt_extension_protocol(
    business_profile_contract,
):
    contract = business_profile_contract
    assert inspect.isclass(contract)
    assert getattr(contract, "_is_protocol", False), "BusinessProfile must extend Protocol"

    declared = set(vars(contract)) | set(getattr(contract, "__annotations__", {}))
    assert {
        "profile_key",
        "profile_version",
        "prompt_version",
        "select_prompt_metadata",
        "build_prompt_extensions",
    }.issubset(declared)

    select_signature = inspect.signature(vars(contract)["select_prompt_metadata"])
    assert "metadata" in select_signature.parameters
    build_signature = inspect.signature(vars(contract)["build_prompt_extensions"])
    assert {"submission_snapshot", "document_snapshot"}.issubset(
        build_signature.parameters
    )


@requires_business_profile
def test_test_profile_is_structurally_compatible(
    business_profile_contract,
    technical_proposal_profile,
):
    contract = business_profile_contract
    profile = technical_proposal_profile
    required = set(vars(contract)) | set(getattr(contract, "__annotations__", {}))
    for name in required:
        if not name.startswith("_"):
            assert hasattr(profile, name), f"TestProfile lacks BusinessProfile.{name}"
    if getattr(contract, "_is_runtime_protocol", False):
        assert isinstance(profile, contract)


@requires_profile_snapshot_wiring
def test_test_profile_directly_consumes_the_real_m2_snapshot_dtos(
    technical_proposal_profile,
):
    _PROFILE_PROBE.require("BusinessProfile")
    submission_type = _CONTRACT_PROBE.require("SubmissionSnapshot")
    document_type = _CONTRACT_PROBE.require("DocumentSnapshot")
    submission = submission_type.from_mapping(technical_submission_payload())
    document = document_type.from_mapping(technical_document_payload())

    extensions = technical_proposal_profile.build_prompt_extensions(
        submission_snapshot=submission,
        document_snapshot=document,
    )

    assert extensions["section_semantics"]
    assert extensions["profile_extensions"]["risk_owner_required"] is True


@requires_prompt_envelope_v2
def test_selected_metadata_changes_the_real_profile_prompt_envelope_hash(
    technical_proposal_profile,
):
    envelope_type = _CONTRACT_PROBE.require("PromptEnvelopeV2")
    document = _document_snapshot()
    first_submission = _submission_snapshot()
    changed_submission = _submission_snapshot(
        metadata={
            **first_submission["metadata"],
            "project_name": "不同项目",
            "vendor_name": "Different Vendor",
        }
    )

    def envelope_for(profile, submission):
        extensions = profile.build_prompt_extensions(
            submission_snapshot=submission,
            document_snapshot=document,
        )
        payload = prompt_envelope_payload()
        assert payload["prompt_version"] != M1_PROMPT_VERSION, (
            "PromptEnvelopeV2 changes provider input and must bump PROMPT_VERSION"
        )
        payload["schema_version"] = "prompt-envelope@2"
        payload["profile"] = {
            "key": profile.profile_key,
            "prompt_version": profile.prompt_version,
        }
        payload["profile_prompt_extensions"] = _thaw(extensions)
        return envelope_type.from_mapping(payload)

    default_first = envelope_for(technical_proposal_profile, first_submission)
    default_changed = envelope_for(technical_proposal_profile, changed_submission)
    assert default_first.canonical_hash() == default_changed.canonical_hash()

    allowlisted = replace(
        technical_proposal_profile,
        prompt_metadata_allowlist=frozenset({"project_name"}),
    )
    selected_first = envelope_for(allowlisted, first_submission)
    selected_changed = envelope_for(allowlisted, changed_submission)
    assert selected_first.to_mapping()["profile_prompt_extensions"]["metadata"] == {
        "project_name": "智能排产平台"
    }
    assert selected_first.canonical_hash() != selected_changed.canonical_hash()


def test_test_profile_and_manifest_are_recursively_immutable(
    technical_proposal_profile,
):
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        technical_proposal_profile.profile_version = "changed-in-place"

    manifest = _checker_manifest()
    with pytest.raises(TypeError):
        manifest["new-checker"] = {}
    with pytest.raises(TypeError):
        manifest["technical_proposal.required_sections.v1"]["checker_version"] = "2"


@requires_generic_policy
def test_generic_technical_policy_is_compiler_produced_frozen_and_replayable(
    technical_policy,
):
    assert is_dataclass(technical_policy)
    assert technical_policy.__dataclass_params__.frozen is True
    assert technical_policy.policy_key == "technical_proposal_contract_policy"
    assert technical_policy.usage == "authoritative_new_runs"
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        technical_policy.policy_hash = "0" * 64


def test_metadata_is_excluded_by_default_and_only_allowlisted_fields_enter_prompt(
    technical_proposal_profile,
    frozen_policy_identity,
):
    document = _document_snapshot()
    first = _submission_snapshot()
    changed_unselected = _submission_snapshot(
        metadata={
            "proposal_id": "TP-OTHER",
            "vendor_name": "Different Vendor",
            "project_name": "Different Project",
            "uploader_email": "different@example.invalid",
            "internal_tracking_tag": "south-region",
            "student_id": "another-unselected-reference",
        }
    )

    def projection(profile, submission):
        return _assemble_core_prompt_projection(
            profile=profile,
            policy=frozen_policy_identity,
            checker_manifest=_checker_manifest(),
            submission_snapshot=submission,
            document_snapshot=document,
        )

    baseline = projection(technical_proposal_profile, first)
    changed = projection(technical_proposal_profile, changed_unselected)
    assert _thaw(baseline["profile_prompt_extensions"]["metadata"]) == {}
    assert _canonical_hash(baseline) == _canonical_hash(changed)

    allowlisted = replace(
        technical_proposal_profile,
        prompt_metadata_allowlist=frozenset({"project_name"}),
    )
    selected = projection(allowlisted, first)
    selected_changed = projection(allowlisted, changed_unselected)
    assert _thaw(selected["profile_prompt_extensions"]["metadata"]) == {
        "project_name": "智能排产平台"
    }
    assert _canonical_hash(selected) != _canonical_hash(selected_changed)


def test_core_prompt_projection_binds_snapshot_versions_hashes_policy_and_registry(
    technical_proposal_profile,
    frozen_policy_identity,
):
    document = _document_snapshot()
    projection = _assemble_core_prompt_projection(
        profile=technical_proposal_profile,
        policy=frozen_policy_identity,
        checker_manifest=_checker_manifest(),
        submission_snapshot=_submission_snapshot(),
        document_snapshot=document,
    )
    value = _thaw(projection)
    assert value["document"] == {
        "schema_version": "document-snapshot@1",
        "profile_version": "technical-proposal-test-profile@1",
        "parser_version": "technical-proposal-parser@1",
        "normalizer_version": "technical-proposal-normalizer@1",
        "normalized_content_hash": document["content_hash"],
        "document_snapshot_hash": document["document_snapshot_hash"],
    }
    assert value["policy"]["policy_hash"] == frozen_policy_identity.policy_hash
    assert set(value["checker_manifest"]) == {
        "technical_proposal.required_sections.v1"
    }
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for excluded in (
        "proposal-2026-001",
        "artifact_refs",
        "uploader_email",
        "internal_tracking_tag",
        "student_id",
    ):
        assert excluded not in serialized


@pytest.mark.parametrize(
    "identity_part",
    (
        "profile",
        "policy",
        "registry",
        "document-schema",
        "parser",
        "normalizer",
        "content",
        "document-snapshot",
    ),
)
def test_profile_policy_registry_and_document_identities_are_isolated(
    identity_part,
    technical_proposal_profile,
    frozen_policy_identity,
):
    profile = technical_proposal_profile
    policy = frozen_policy_identity
    manifest = _checker_manifest()
    document = _document_snapshot()
    baseline = _assemble_core_prompt_projection(
        profile=profile,
        policy=policy,
        checker_manifest=manifest,
        submission_snapshot=_submission_snapshot(),
        document_snapshot=document,
    )

    if identity_part == "profile":
        profile = replace(profile, profile_version="technical-proposal-test-profile@2")
        document["profile_version"] = profile.profile_version
    elif identity_part == "policy":
        policy = replace(policy, policy_hash="e" * 64)
    elif identity_part == "registry":
        manifest = _checker_manifest(version="1.1.0")
    elif identity_part == "document-schema":
        document["schema_version"] = "document-snapshot@2"
        # Invalid schema must fail before a prompt identity can be produced.
        with pytest.raises(ValueError, match="DocumentSnapshot schema"):
            _assemble_core_prompt_projection(
                profile=profile,
                policy=policy,
                checker_manifest=manifest,
                submission_snapshot=_submission_snapshot(),
                document_snapshot=document,
            )
        return
    elif identity_part == "parser":
        document["parser_version"] = "technical-proposal-parser@2"
    elif identity_part == "normalizer":
        document["normalizer_version"] = "technical-proposal-normalizer@2"
    elif identity_part == "content":
        document["content_hash"] = "e" * 64
    else:
        document["document_snapshot_hash"] = "f" * 64

    changed = _assemble_core_prompt_projection(
        profile=profile,
        policy=policy,
        checker_manifest=manifest,
        submission_snapshot=_submission_snapshot(),
        document_snapshot=document,
    )
    assert _canonical_hash(changed) != _canonical_hash(baseline)


def test_technical_profile_has_no_built_in_thesis_schema_or_checker_leakage(
    technical_proposal_profile,
    frozen_policy_identity,
):
    with pytest.raises(ValueError, match="thesis-only field"):
        replace(
            technical_proposal_profile,
            prompt_metadata_allowlist=frozenset({"student_id"}),
        )
    with pytest.raises(ValueError, match="only supports technical_proposal"):
        replace(technical_proposal_profile, profile_key="thesis")

    # Input text may legitimately mention a patent abstract or a student user;
    # the boundary forbids built-in thesis schema/output, not ordinary words.
    document = _document_snapshot()
    document["sections"][0]["normalized_text"] = "专利摘要作为外部需求输入。"
    projection = _assemble_core_prompt_projection(
        profile=technical_proposal_profile,
        policy=frozen_policy_identity,
        checker_manifest=_checker_manifest(),
        submission_snapshot=_submission_snapshot(),
        document_snapshot=document,
    )
    flattened_keys = set()

    def collect_keys(value):
        if isinstance(value, Mapping):
            for key, item in value.items():
                flattened_keys.add(str(key))
                collect_keys(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect_keys(item)

    collect_keys(projection)
    assert not (_THESIS_ONLY_FIELDS & flattened_keys)
    assert all(not key.startswith("thesis.") for key in projection["checker_manifest"])


def test_test_profile_rejects_snapshot_profile_or_schema_mismatch(
    technical_proposal_profile,
):
    submission = _submission_snapshot()
    document = _document_snapshot()
    submission["profile_key"] = "another_profile"
    with pytest.raises(ValueError, match="submission snapshot profile"):
        technical_proposal_profile.build_prompt_extensions(
            submission_snapshot=submission,
            document_snapshot=document,
        )

    submission = _submission_snapshot()
    document["profile_version"] = "technical-proposal-test-profile@999"
    with pytest.raises(ValueError, match="profile version"):
        technical_proposal_profile.build_prompt_extensions(
            submission_snapshot=submission,
            document_snapshot=document,
        )
