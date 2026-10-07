"""Decide Chat Completions vs Responses for a private AI connection.

Users fill in a base URL; both protocols hang off the same prefix
(``<base>/chat/completions`` and ``<base>/responses``), so the URL alone rarely
says which one to use.  Resolution runs once, when a connection is tested or
created, and the result is stored in the existing ``provider_type`` column:

1. an explicit choice from the advanced settings always wins (``manual``);
2. a pasted full endpoint ending in ``/chat/completions`` or ``/responses``
   decides the protocol and is trimmed back to the base URL (``url_suffix``);
3. known hosts give the preferred protocol — OpenAI's own API prefers
   Responses, every other listed platform uses Chat Completions (``known_host``);
4. otherwise a minimal probe tries Chat first, then Responses (``probe``).

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
AUTO = "auto"
PROTOCOLS = (CHAT, RESPONSES)

_ENDPOINT_SUFFIXES = (
    ("/chat/completions", CHAT),
    ("/responses", RESPONSES),
)

# Host (or parent domain) -> preferred protocol.  The table only picks the
# first protocol to try; a stale entry costs one extra probe when testing, never
# a wrong silent choice for a tested connection.  Keep it to platforms whose
# Chat Completions support is well established.
KNOWN_HOSTS = {
    "api.openai.com": RESPONSES,
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
    source: str  # manual | url_suffix | known_host | probe
    verified: bool


def split_endpoint_suffix(base_url: str) -> tuple[str, str | None]:
    """Trim a pasted full endpoint back to its base URL."""

    stripped = base_url.rstrip("/")
    lowered = stripped.casefold()
    for suffix, protocol in _ENDPOINT_SUFFIXES:
        if lowered.endswith(suffix):
            return stripped[: -len(suffix)].rstrip("/"), protocol
    return stripped, None


def known_host_protocol(base_url: str) -> str | None:
    host = (urlsplit(base_url).hostname or "").rstrip(".").casefold()
    for domain, protocol in KNOWN_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return protocol
    return None


def _other(protocol: str) -> str:
    return RESPONSES if protocol == CHAT else CHAT


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
        if verify:
            probe(requested, base)
        return ProtocolResolution(requested, base, "manual", verify)
    if requested != AUTO:
        raise ValueError("unsupported AI provider type")
    if suffix_protocol:
        if verify:
            probe(suffix_protocol, base)
        return ProtocolResolution(suffix_protocol, base, "url_suffix", verify)
    preferred = known_host_protocol(base)
    if preferred and not verify:
        return ProtocolResolution(preferred, base, "known_host", False)
    candidates = (preferred, _other(preferred)) if preferred else (CHAT, RESPONSES)
    for index, candidate in enumerate(candidates):
        try:
            probe(candidate, base)
        except ProtocolEndpointMissing:
            continue
        source = "known_host" if preferred and index == 0 else "probe"
        return ProtocolResolution(candidate, base, source, True)
    raise ProtocolNotDetected(
        "该接口地址下没有找到 Chat Completions 或 Responses 接口，请检查接口地址，"
        "或在高级设置中手动选择协议。"
    )


__all__ = [
    "AUTO",
    "CHAT",
    "KNOWN_HOSTS",
    "PROTOCOLS",
    "RESPONSES",
    "ProtocolEndpointMissing",
    "ProtocolNotDetected",
    "ProtocolResolution",
    "known_host_protocol",
    "resolve_protocol",
    "split_endpoint_suffix",
]
