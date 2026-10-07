"""Type-aware, budgeted, verbatim-only evidence compression for provider views.

Selected evidence units are shown to the model as **contiguous verbatim
fragments** of their original ``normalized_text`` joined by ``…``.  Quote
validation still runs against the original text, so a quote must sit inside
one fragment; compression cannot weaken the anti-fabrication check.

Every unit gets a recorded decision (kind, reducer, chars before/after, reason)
so a request can be audited and reproduced from its envelope and
``COMPRESSOR_VERSION`` alone.
"""

from __future__ import annotations

import re

from backend.app.services.scoring.retrieval.selection import query_terms


COMPRESSOR_VERSION = "evidence-compressor@1"
UNIT_CHAR_CAP = 400
EVIDENCE_CHAR_CAP = 3000
MIN_UNIT_CHARS = 40
ELLIPSIS = "…"

_REFERENCE_RE = re.compile(r"^\s*\[\d+\]")
_CAPTION_RE = re.compile(r"^\s*(图|表|Figure|Table|Fig\.)\s*\d+([.\-．]\d+)*")
_PERSONAL_RE = re.compile(
    r"(学生姓名|姓\s*名|学\s*号|学生学号|指导教师|指导老师|导\s*师|身份证|联系电话|手机号)"
)
_SENTENCE_RE = re.compile(r"[^。！？；!?;\n]+[。！？；!?;\n]?")
_CODE_MARKERS = re.compile(r"[{};]|\b(def|class|public|private|import|return|function)\b")


def classify_unit(text: str) -> str:
    stripped = text.strip()
    if _PERSONAL_RE.search(stripped) and len(stripped) <= 80:
        return "personal"
    if _REFERENCE_RE.match(stripped):
        return "reference"
    if _CAPTION_RE.match(stripped) and len(stripped) <= 60:
        return "caption"
    if stripped.count(" | ") >= 2:
        return "table"
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 3 and sum(bool(_CODE_MARKERS.search(line)) for line in lines) >= 2:
        return "code"
    return "paragraph"


def _join(text: str, spans: list[tuple[int, int]]) -> str:
    """Join kept spans in document order; ``…`` marks every omission."""

    parts = []
    cursor = 0
    for start, end in sorted(spans):
        if start > cursor:
            parts.append(ELLIPSIS)
        parts.append(text[start:end].strip())
        cursor = end
    if cursor < len(text.rstrip()):
        parts.append(ELLIPSIS)
    joined = "".join(parts)
    return joined.replace(ELLIPSIS + ELLIPSIS, ELLIPSIS)


def _segments(text: str, kind: str) -> list[tuple[int, int]]:
    if kind in {"table", "code"}:
        spans = []
        start = 0
        for line in text.splitlines(keepends=True):
            if line.strip():
                spans.append((start, start + len(line.rstrip("\n"))))
            start += len(line)
        return spans
    return [match.span() for match in _SENTENCE_RE.finditer(text) if match.group().strip()]


def _score(segment: str, terms: set[str]) -> int:
    lowered = segment.casefold()
    return sum(1 for term in terms if term in lowered)


def compress_unit(text: str, *, kind: str, terms: set[str], cap: int) -> tuple[str, str]:
    """Return (shown_text, reducer).  ``shown_text`` is verbatim fragments only."""

    if len(text) <= cap:
        return text, "kept"
    segments = _segments(text, kind)
    if not segments:
        return text[:cap].rstrip() + ELLIPSIS, "head"
    # The first segment carries a paragraph's topic sentence, a table's header
    # and a code block's signature; the last carries conclusions.
    keep = {0}
    if kind == "paragraph" and len(segments) > 1:
        keep.add(len(segments) - 1)
    ranked = sorted(
        range(len(segments)),
        key=lambda index: (-_score(text[segments[index][0]:segments[index][1]], terms), index),
    )

    def size(indices):
        return sum(segments[i][1] - segments[i][0] for i in indices) + len(indices)

    if size(keep) > cap:
        keep = {0}
    for index in ranked:
        if index in keep:
            continue
        if size(keep | {index}) > cap:
            continue
        keep.add(index)
    if size(keep) > cap:
        start, _end = segments[0]
        return text[start:start + cap].rstrip() + ELLIPSIS, "head"
    reducer = {"table": "table_rows", "code": "code_lines"}.get(kind, "sentences")
    return _join(text, [segments[index] for index in keep]), reducer


def compress_evidence(units, *, query: str, keep_captions: bool):
    """Compress units (already in relevance order) under per-unit and total caps.

    Returns ``(shown, decisions)`` where ``shown`` lists ``(unit, text)`` pairs
    for the units that remain visible.
    """

    terms = set(query_terms(query))
    remaining = EVIDENCE_CHAR_CAP
    shown = []
    decisions = []
    for unit in units:
        text = unit["normalized_text"]
        kind = classify_unit(text)
        decision = {
            "evidence_unit_id": unit["evidence_unit_id"],
            "kind": kind,
            "before_chars": len(text),
        }
        if kind == "personal":
            decisions.append({**decision, "reducer": "dropped", "after_chars": 0, "reason": "personal_information"})
            continue
        if kind == "caption" and not keep_captions:
            decisions.append({**decision, "reducer": "dropped", "after_chars": 0, "reason": "caption_not_needed"})
            continue
        cap = min(UNIT_CHAR_CAP, remaining)
        if cap < MIN_UNIT_CHARS:
            decisions.append({**decision, "reducer": "dropped", "after_chars": 0, "reason": "evidence_budget"})
            continue
        shown_text, reducer = compress_unit(text, kind=kind, terms=terms, cap=cap)
        remaining -= len(shown_text)
        shown.append((unit, shown_text))
        decisions.append(
            {
                **decision,
                "reducer": reducer,
                "after_chars": len(shown_text),
                "reason": "within_cap" if reducer == "kept" else "unit_cap",
            }
        )
    return shown, decisions


def verbatim_fragments(shown_text: str) -> list[str]:
    return [fragment for fragment in shown_text.split(ELLIPSIS) if fragment]


__all__ = [
    "COMPRESSOR_VERSION",
    "ELLIPSIS",
    "EVIDENCE_CHAR_CAP",
    "UNIT_CHAR_CAP",
    "classify_unit",
    "compress_evidence",
    "compress_unit",
    "verbatim_fragments",
]
