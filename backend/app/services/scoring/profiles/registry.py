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

#: 工作台（v1 批次）能评的文档类型。技术方案只走 v2 接口：它没有工作台评分路径
#: 需要的论文适配方法，还有自己的厂商、项目字段（通用文档评分改造方案 G5）。
WORKBENCH_PROFILE_KEYS = ("thesis", "general_document")
#: 未版本化的旧评分标准与旧批次没有类型记录，一律按论文处理——它们只走论文路径。
LEGACY_PROFILE_KEY = "thesis"


def profile_key_for_version(version) -> str:
    """评分标准版本的文档类型；没有正式版本时视为论文。"""

    key = getattr(version, "business_profile_key", None) if version is not None else None
    return key or LEGACY_PROFILE_KEY


def profile_key_for_batch(batch) -> str:
    """批次的文档类型 = 锁定版本的类型（通用文档评分改造方案 §4.1）。"""

    return profile_key_for_version(getattr(batch, "rubric_version", None))


def get_workbench_profile(profile_key: str):
    """工作台（v1 批次）用的 Profile：评分、解析、导出、报告共用这一处。

    论文直接构造 `ThesisProfile`，与改造前逐字节一致（不受测试里临时注册的替身影响）；
    其它类型从注册表取，只接受 `WORKBENCH_PROFILE_KEYS`。
    """

    if profile_key == LEGACY_PROFILE_KEY:
        from backend.app.services.scoring.profiles.thesis import ThesisProfile

        return ThesisProfile()
    if profile_key not in WORKBENCH_PROFILE_KEYS:
        raise ValueError(
            "business profile %s cannot be used in a grading batch" % profile_key
        )
    return get_profile_by_key(profile_key)


def _default_profile(profile_key: str):
    if profile_key == "thesis":
        from backend.app.services.scoring.profiles.thesis import ThesisProfile

        return ThesisProfile()
    if profile_key == "general_document":
        from backend.app.services.scoring.profiles.general_document import (
            GeneralDocumentProfile,
        )

        return GeneralDocumentProfile()
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
    "LEGACY_PROFILE_KEY",
    "WORKBENCH_PROFILE_KEYS",
    "get_profile",
    "get_profile_by_key",
    "get_workbench_profile",
    "profile_key_for_batch",
    "profile_key_for_version",
    "register_profile",
    "temporary_profile_registration",
]
