"""Immutable, explicit checker registrations used by executable plans.

The registry never imports the manifest entrypoint.  Deployment code must
provide the callable and parameter validator explicitly, so a published plan
cannot turn database text into an import side effect.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from types import MappingProxyType

from backend.app.services.scoring.core.canonical import canonical_sha256


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSIONED_KEY = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\.v[1-9][0-9]*$")
_FIELDS = {
    "schema_version",
    "scheme",
    "checker_key",
    "checker_version",
    "entrypoint",
    "runtime_contract_version",
    "dependency_manifest_hash",
    "artifact_manifest",
    "implementation_hash",
    "params_schema",
    "supported_document_schemas",
    "supported_profiles",
    "observation_schema",
}
_MANIFEST_FIELDS = (
    "checker_version",
    "implementation_hash",
    "params_schema",
    "supported_document_schemas",
    "supported_profiles",
    "observation_schema",
)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _sha256(value: object, path: str) -> str:
    text = _text(value, path)
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{path} must be a lowercase SHA-256 digest")
    return text


def _text_list(value: object, path: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{path} must be a non-empty array")
    result = [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise ValueError(f"{path} must not contain duplicates")
    return sorted(result)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _package_projection(registration: Mapping[str, object]) -> dict[str, object]:
    return {
        "scheme": "checker-package-sha256-v1",
        "checker_key": registration["checker_key"],
        "checker_version": registration["checker_version"],
        "entrypoint": registration["entrypoint"],
        "runtime_contract_version": registration["runtime_contract_version"],
        "dependency_manifest_hash": registration["dependency_manifest_hash"],
        "artifact_manifest": registration["artifact_manifest"],
    }


def _normalize_registration(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("checker registration must be an object")
    unknown = set(value) - _FIELDS
    missing = _FIELDS - set(value)
    if unknown:
        raise ValueError(f"checker registration contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"checker registration is missing fields: {sorted(missing)}")
    if value["schema_version"] != "checker-registration@1":
        raise ValueError("unsupported checker registration schema_version")

    checker_key = _text(value["checker_key"], "checker_key")
    if not _VERSIONED_KEY.fullmatch(checker_key):
        raise ValueError("checker_key must be namespaced and explicitly versioned")
    artifacts = value["artifact_manifest"]
    if not isinstance(artifacts, (list, tuple)) or not artifacts:
        raise ValueError("artifact_manifest must be a non-empty array")
    normalized_artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        path = f"artifact_manifest[{index}]"
        if not isinstance(artifact, Mapping) or set(artifact) != {"path", "sha256"}:
            raise ValueError(f"{path} must contain only path and sha256")
        artifact_path = _text(artifact["path"], path + ".path")
        path_parts = artifact_path.split("/")
        if (
            artifact_path.startswith("/")
            or "\\" in artifact_path
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            raise ValueError(f"{path}.path must be a package-relative artifact path")
        if artifact_path in seen_paths:
            raise ValueError("artifact_manifest contains duplicate artifact paths")
        seen_paths.add(artifact_path)
        normalized_artifacts.append(
            {"path": artifact_path, "sha256": _sha256(artifact["sha256"], path + ".sha256")}
        )
    normalized_artifacts.sort(key=lambda item: item["path"])

    normalized: dict[str, object] = {
        "schema_version": "checker-registration@1",
        "scheme": _text(value["scheme"], "scheme"),
        "checker_key": checker_key,
        "checker_version": _text(value["checker_version"], "checker_version"),
        "entrypoint": _text(value["entrypoint"], "entrypoint"),
        "runtime_contract_version": _text(
            value["runtime_contract_version"], "runtime_contract_version"
        ),
        "dependency_manifest_hash": _sha256(
            value["dependency_manifest_hash"], "dependency_manifest_hash"
        ),
        "artifact_manifest": normalized_artifacts,
        "implementation_hash": _sha256(
            value["implementation_hash"], "implementation_hash"
        ),
        "params_schema": _text(value["params_schema"], "params_schema"),
        "supported_document_schemas": _text_list(
            value["supported_document_schemas"], "supported_document_schemas"
        ),
        "supported_profiles": _text_list(
            value["supported_profiles"], "supported_profiles"
        ),
        "observation_schema": _text(
            value["observation_schema"], "observation_schema"
        ),
    }
    if normalized["scheme"] != "checker-package-sha256-v1":
        raise ValueError("unsupported checker package hash scheme")
    expected_hash = canonical_sha256(_package_projection(normalized))
    if normalized["implementation_hash"] != expected_hash:
        raise ValueError("checker implementation_hash does not match package manifest")
    return normalized


class VersionedCheckerRegistry:
    """Process-local registry whose versioned keys can be registered once."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[dict[str, object], Callable, Callable]] = {}

    def register(self, *, registration, checker, validate_params) -> None:
        if not callable(checker):
            raise TypeError("checker must be callable")
        if not callable(validate_params):
            raise TypeError("checker parameter validator must be callable")
        normalized = _normalize_registration(registration)
        key = str(normalized["checker_key"])
        if key in self._entries:
            raise ValueError(f"checker key {key!r} already exists and is immutable")
        self._entries[key] = (deepcopy(normalized), checker, validate_params)

    def resolve(
        self,
        *,
        checker_key: str,
        checker_version: str,
        checker_params,
        profile_key: str,
        document_schema_version: str,
    ):
        try:
            registration, checker, validate_params = self._entries[checker_key]
        except KeyError as exc:
            raise KeyError(f"unknown checker key: {checker_key}") from exc
        if checker_version != registration["checker_version"]:
            raise KeyError(f"unknown checker version: {checker_key}@{checker_version}")
        if profile_key not in registration["supported_profiles"]:
            raise ValueError(f"checker {checker_key} does not support profile {profile_key}")
        if document_schema_version not in registration["supported_document_schemas"]:
            raise ValueError(
                f"checker {checker_key} does not support document schema "
                f"{document_schema_version}"
            )
        try:
            validate_params(deepcopy(checker_params))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid checker params for {checker_key}: {exc}") from exc
        return checker

    def manifest(self, *, profile_key: str, document_schema_version: str):
        result: dict[str, dict[str, object]] = {}
        for key, (registration, _checker, _validator) in sorted(self._entries.items()):
            if profile_key not in registration["supported_profiles"]:
                continue
            if document_schema_version not in registration["supported_document_schemas"]:
                continue
            result[key] = {
                field: deepcopy(registration[field]) for field in _MANIFEST_FIELDS
            }
        return _freeze(result)


__all__ = ["VersionedCheckerRegistry"]
