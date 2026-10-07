"""Deterministic paper digest ("概况卡") for provider views.

Built from the parsed document snapshot without any model call.  It lets a
semantic judge see the paper's shape (outline, size, figures, references, key
chapters, failed structure checks) and decide whether a rule's premise holds.

The digest is orientation only, never evidence: deductions must still quote an
evidence unit verbatim.  It replaces sending the full reference list or every
structure check to every rule.
"""

from __future__ import annotations

from collections import Counter
import re


DIGEST_VERSION = "paper-digest@1"
MAX_OUTLINE_CHARS = 360
MAX_HEADING_CHARS = 24
_NUMBERING_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_NOISE_HEADINGS = {"未命名开头"}

_YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
_TYPE_RE = re.compile(r"\[(J|M|D|C|N|P|S|R|EB/OL|DB/OL|Z)\]", re.IGNORECASE)
_FIGURE_RE = re.compile(r"^\s*图\s*\d+")
_TABLE_RE = re.compile(r"^\s*表\s*\d+")
_KEY_SECTIONS = {
    "requirements": ("需求分析", "需求"),
    "design": ("设计",),
    "implementation": ("实现",),
    "testing": ("测试",),
    "conclusion": ("总结", "结论", "结束语"),
}


def _reference_summary(references) -> dict:
    entries = [str(item) for item in references or () if str(item).strip()]
    years = sorted(int(match.group(1)) for entry in entries for match in [_YEAR_RE.search(entry)] if match)
    types = Counter(
        match.group(1).upper() for entry in entries for match in [_TYPE_RE.search(entry)] if match
    )
    summary = {"count": len(entries), "types": dict(sorted(types.items()))}
    if years:
        summary["year_range"] = [years[0], years[-1]]
        latest = years[-1]
        summary["within_5_years_of_latest"] = sum(1 for year in years if latest - year <= 5)
    return summary


def _heading_level(heading: str) -> int | None:
    """1 = chapter or unnumbered top heading, 2 = "N.M", None = drop."""

    if not heading or heading in _NOISE_HEADINGS or " | " in heading:
        return None
    match = _NUMBERING_RE.match(heading)
    if match is None:
        return 1
    depth = match.group(1).count(".") + 1
    return depth if depth <= 2 else None


def _outline(sections) -> list[str]:
    """Every chapter, then second-level headings in document order up to a cap."""

    candidates = []
    seen = set()
    for section in sections:
        heading = str(section.get("heading") or "").strip()
        level = _heading_level(heading)
        if level is None or heading in seen:
            continue
        seen.add(heading)
        candidates.append((len(candidates), level, heading[:MAX_HEADING_CHARS]))
    chosen = [item for item in candidates if item[1] == 1]
    used = sum(len(item[2]) for item in chosen)
    for item in candidates:
        if item[1] == 2 and used + len(item[2]) <= MAX_OUTLINE_CHARS:
            chosen.append(item)
            used += len(item[2])
    return [heading for _index, _level, heading in sorted(chosen)]


def build_paper_digest(*, document_snapshot, profile_extensions=None, title: str = "") -> dict:
    sections = list(document_snapshot.get("sections") or ())
    outline = _outline(sections)
    units = list(document_snapshot.get("evidence_units") or ())
    headings = " ".join(str(section.get("heading") or "") for section in sections)
    extensions = profile_extensions or {}
    digest = {
        "schema_version": DIGEST_VERSION,
        "title": str(title or "")[:60],
        "outline": outline,
        "body_chars": sum(len(section.get("normalized_text") or "") for section in sections),
        "figure_captions": sum(1 for unit in units if _FIGURE_RE.match(unit.get("normalized_text") or "")),
        "table_captions": sum(1 for unit in units if _TABLE_RE.match(unit.get("normalized_text") or "")),
        "references": _reference_summary(extensions.get("references")),
        "key_sections": {
            key: any(marker in headings for marker in markers)
            for key, markers in _KEY_SECTIONS.items()
        },
        "failed_structure_checks": [
            str(check.get("name") or check.get("code"))
            for check in extensions.get("structure_checks") or ()
            if isinstance(check, dict) and check.get("passed") is False
        ],
    }
    return digest


__all__ = ["DIGEST_VERSION", "build_paper_digest"]
