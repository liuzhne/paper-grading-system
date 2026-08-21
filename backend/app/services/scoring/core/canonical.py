"""Minimal deterministic JSON used for content-addressed scoring identities.

The serializer is deliberately smaller than ``json.dumps(default=...)``.  A
cache identity must never depend on an implicit conversion of a float, a set,
an ORM object, or another process-local representation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("canonical JSON does not support non-finite Decimal values")
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _normalize(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("canonical JSON does not support float values; use Decimal or a string")
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise TypeError("unsupported canonical JSON value: %s" % type(value).__name__)


def canonical_json(value) -> str:
    """Return compact, UTF-8-preserving, key-sorted canonical JSON text."""

    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value) -> str:
    """Hash the exact UTF-8 bytes emitted by :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = ["canonical_json", "canonical_sha256"]
