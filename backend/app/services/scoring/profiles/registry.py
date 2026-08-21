"""Explicit in-process registry for business Profile capabilities.

Database rows choose the profile key/version for a batch. This registry only
maps that frozen identity to repository code; it never invents a business
association or silently falls back to another profile.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock


_LOCK = RLock()
_REGISTERED = {}


def _default_profile(profile_key: str):
    if profile_key == "thesis":
        from backend.app.services.scoring.profiles.thesis import ThesisProfile

        return ThesisProfile()
    if profile_key == "technical_proposal":
        from backend.app.services.scoring.profiles.technical_proposal import (
            TechnicalProposalProfile,
        )

        return TechnicalProposalProfile()
    return None


def get_profile_by_key(profile_key: str):
    with _LOCK:
        profile = _REGISTERED.get(profile_key)
    if profile is None:
        profile = _default_profile(profile_key)
    if profile is None:
        raise LookupError("business profile is not registered")
    if getattr(profile, "profile_key", None) != profile_key:
        raise ValueError("registered business profile key mismatch")
    return profile


def get_profile(*, profile_key: str, profile_version: str):
    profile = get_profile_by_key(profile_key)
    if getattr(profile, "profile_version", None) != profile_version:
        raise ValueError("registered business profile version mismatch")
    return profile


def register_profile(profile) -> None:
    profile_key = getattr(profile, "profile_key", None)
    profile_version = getattr(profile, "profile_version", None)
    if not isinstance(profile_key, str) or not profile_key.strip():
        raise ValueError("business profile key must be non-empty")
    if not isinstance(profile_version, str) or not profile_version.strip():
        raise ValueError("business profile version must be non-empty")
    with _LOCK:
        existing = _REGISTERED.get(profile_key)
        if existing is not None and existing is not profile:
            raise ValueError("business profile key is already registered")
        _REGISTERED[profile_key] = profile


@contextmanager
def temporary_profile_registration(profile):
    """Temporarily replace one key; intended for isolated contract tests."""

    profile_key = getattr(profile, "profile_key", None)
    if not isinstance(profile_key, str) or not profile_key.strip():
        raise ValueError("business profile key must be non-empty")
    with _LOCK:
        previous = _REGISTERED.get(profile_key)
        _REGISTERED[profile_key] = profile
    try:
        yield profile
    finally:
        with _LOCK:
            if previous is None:
                _REGISTERED.pop(profile_key, None)
            else:
                _REGISTERED[profile_key] = previous


__all__ = [
    "get_profile",
    "get_profile_by_key",
    "register_profile",
    "temporary_profile_registration",
]
