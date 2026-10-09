"""Decide Chat Completions vs Responses vs Anthropic Messages for an AI connection.

Users fill in a base URL; the protocols hang off the same prefix
(``<base>/chat/completions``, ``<base>/responses``, ``<base>/messages``), so the
URL alone rarely says which one to use.  Resolution runs once, when a connection
is tested or created, and the result is stored in the existing ``provider_type``
column:

1. an explicit choice from the advanced settings always wins (``manual``);
2. a pasted full endpoint ending in ``/chat/completions``, ``/responses`` or
   ``/messages`` decides the protocol and is trimmed back to the base URL
   (``url_suffix``);
3. a path with an ``/anthropic`` segment is an Anthropic Messages endpoint —
   both Bedrock hosts and the Anthropic-compatible endpoints of other vendors
   (``url_path``).  This runs before the host table: ``api.deepseek.com`` is a
   Chat host, but ``api.deepseek.com/anthropic`` is not;
4. known hosts give the preferred protocol — OpenAI's own API prefers
   Responses, Anthropic's own API uses Messages, every other listed platform
   uses Chat Completions (``known_host``).  Bedrock hosts are not listed: they
   also serve OpenAI-compatible endpoints, so the host alone decides nothing;
5. otherwise a minimal probe tries Chat, then Responses, then Messages
   (``probe``).

Messages base URLs are normalized to end in ``/v1`` (the SDK-style
``…/anthropic`` and the bare ``https://api.anthropic.com`` both mean the
``/v1/messages`` endpoint), so the adapter always posts ``{base}/messages``.

Only "endpoint missing" (404/405) moves on to the other protocol.  Auth,
permission, model, rate-limit, timeout and network errors are reported as they
are: trying the other protocol would just fail the same way a second time.
Scoring never switches protocol at call time; it always uses the stored value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit


CHAT = "openai_compatible"
RESPONSES = "openai_responses"
MESSAGES = "anthropic_messages"
AUTO = "auto"
PROTOCOLS = (CHAT, RESPONSES, MESSAGES)

_ENDPOINT_SUFFIXES = (
    ("/chat/completions", CHAT),
    ("/responses", RESPONSES),
    ("/messages", MESSAGES),
)

# Host (or parent domain) -> preferred protocol.  The table only picks the
# first protocol to try; a stale entry costs one extra probe when testing, never
# a wrong silent choice for a tested connection.  Keep it to platforms whose
# Chat Completions support is well established.
KNOWN_HOSTS = {
    "api.openai.com": RESPONSES,
    "api.anthropic.com": MESSAGES,
    "open.bigmodel.cn": CHAT,
    "api.deepseek.com": CHAT,
    "generativelanguage.googleapis.com": CHAT,
    "api.moonshot.cn": CHAT,
    "api.groq.com": CHAT,
    "openrouter.ai": CHAT,
    "dashscope.aliyuncs.com": CHAT,
    "dashscope-intl.aliyuncs.com": CHAT,
    "maas.aliyuncs.com": CHAT,
    "ark.cn-beijing.volces.com": CHAT,
    "api.siliconflow.cn": CHAT,
}


class ProtocolEndpointMissing(ValueError):
    """The probed protocol's endpoint does not exist at this base URL (404/405)."""


class ProtocolNotDetected(ValueError):
    """Neither protocol answered at this base URL."""


@dataclass(frozen=True)
class ProtocolResolution:
    provider_type: str
    base_url: str
    source: str  # manual | url_suffix | url_path | known_host | probe
    verified: bool


def split_endpoint_suffix(base_url: str) -> tuple[str, str | None]:
    """Trim a pasted full endpoint back to its base URL."""

    stripped = base_url.rstrip("/")
    lowered = stripped.casefold()
    for suffix, protocol in _ENDPOINT_SUFFIXES:
        if lowered.endswith(suffix):
            return stripped[: -len(suffix)].rstrip("/"), protocol
    return stripped, None


def path_protocol(base_url: str) -> str | None:
    """An ``/anthropic`` path segment marks an Anthropic Messages endpoint."""

    segments = [part.casefold() for part in urlsplit(base_url).path.split("/") if part]
    return MESSAGES if "anthropic" in segments else None


def normalize_base_url(provider_type: str, base_url: str) -> str:
    """Messages base URLs always end in ``/v1``; other protocols are untouched."""

    base = base_url.rstrip("/")
    if provider_type != MESSAGES:
        return base
    parts = urlsplit(base)
    segments = [part for part in parts.path.split("/") if part]
    if segments and segments[-1].casefold() == "v1":
        return base
    if not segments or segments[-1].casefold() == "anthropic":
        return base + "/v1"
    return base


def known_host_protocol(base_url: str) -> str | None:
    host = (urlsplit(base_url).hostname or "").rstrip(".").casefold()
    for domain, protocol in KNOWN_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return protocol
    return None


def _resolution(provider_type, base, source, verified):
    return ProtocolResolution(provider_type, normalize_base_url(provider_type, base), source, verified)


def resolve_protocol(
    *,
    requested: str,
    base_url: str,
    verify: bool,
    probe: Callable[[str, str], None],
) -> ProtocolResolution:
    """Return the protocol to store; ``probe(provider_type, base_url)`` raises on failure.

    ``verify=True`` (testing a draft) always sends a probe.  ``verify=False``
    (saving) stays offline whenever the URL or a known host already decides the
    protocol, and only probes unknown hosts.
    """

    base, suffix_protocol = split_endpoint_suffix(base_url)
    if requested in PROTOCOLS:
        base = normalize_base_url(requested, base)
        if verify:
            probe(requested, base)
        return ProtocolResolution(requested, base, "manual", verify)
    if requested != AUTO:
        raise ValueError("unsupported AI provider type")
    decided = (
        (suffix_protocol, "url_suffix") if suffix_protocol
        else (path_protocol(base), "url_path")
    )
    if decided[0]:
        protocol, source = decided
        base = normalize_base_url(protocol, base)
        if verify:
            probe(protocol, base)
        return ProtocolResolution(protocol, base, source, verify)
    preferred = known_host_protocol(base)
    if preferred and not verify:
        return _resolution(preferred, base, "known_host", False)
    candidates = ((preferred,) if preferred else ()) + tuple(
        protocol for protocol in PROTOCOLS if protocol != preferred
    )
    for index, candidate in enumerate(candidates):
        candidate_base = normalize_base_url(candidate, base)
        try:
            probe(candidate, candidate_base)
        except ProtocolEndpointMissing:
            continue
        source = "known_host" if preferred and index == 0 else "probe"
        return ProtocolResolution(candidate, candidate_base, source, True)
    raise ProtocolNotDetected(
        "该接口地址下没有找到 Chat Completions、Responses 或 Anthropic Messages 接口，"
        "请检查接口地址，或在高级设置中手动选择协议。"
    )


__all__ = [
    "AUTO",
    "CHAT",
    "KNOWN_HOSTS",
    "MESSAGES",
    "PROTOCOLS",
    "RESPONSES",
    "ProtocolEndpointMissing",
    "ProtocolNotDetected",
    "ProtocolResolution",
    "known_host_protocol",
    "normalize_base_url",
    "path_protocol",
    "resolve_protocol",
    "split_endpoint_suffix",
]
