import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import PaperChunk
from backend.app.db.models import RubricCriterion


def retrieve_for_criterion(db: Session, paper_id: str, criterion: RubricCriterion, top_k: int = 8):
    """结构化定位优先（设计§7 L1）：applies_to 指向具体章节时取该章节文本块，
    否则回退关键词召回。这护住"证据定位"原则——按章节取是确定的、无漏检。"""
    applies_to = (getattr(criterion, "applies_to", "") or "global").strip()
    if applies_to and applies_to != "global":
        section_chunks = retrieve_section_chunks(db, paper_id, applies_to)
        if section_chunks:
            return section_chunks[:top_k]
    return retrieve_evidence(db, paper_id, criterion, top_k=top_k)


def retrieve_section_chunks(db: Session, paper_id: str, applies_to: str):
    chunks = db.scalars(select(PaperChunk).where(PaperChunk.paper_id == paper_id)).all()
    matched = [
        chunk
        for chunk in chunks
        if chunk.section_title and (applies_to in chunk.section_title or chunk.section_title in applies_to)
    ]
    return [_candidate_from_chunk(chunk) for chunk in matched]


def retrieve_evidence(db: Session, paper_id: str, criterion: RubricCriterion, top_k: int = 8):
    chunks = db.scalars(select(PaperChunk).where(PaperChunk.paper_id == paper_id)).all()
    scored = []
    keywords = _keywords_for_criterion(criterion)

    for chunk in chunks:
        score = 0
        section_title = chunk.section_title or ""
        haystack = "%s\n%s" % (section_title, chunk.text)
        for keyword in keywords:
            if not keyword:
                continue
            if keyword in section_title:
                score += 6
            score += 2 * haystack.count(keyword)
        score += min(len(chunk.text) / 600, 2)
        scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [chunk for score, chunk in scored if score > 0][:top_k]
    if len(selected) < min(3, len(scored)):
        for _, chunk in scored:
            if chunk not in selected:
                selected.append(chunk)
            if len(selected) >= min(top_k, 3):
                break

    return [_candidate_from_chunk(chunk) for chunk in selected[:top_k]]


def _keywords_for_criterion(criterion):
    raw_keywords = [criterion.name]
    raw_keywords.extend(criterion.evidence_hints or [])
    if criterion.description:
        raw_keywords.extend(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", criterion.description))
    keywords = []
    for item in raw_keywords:
        if not item:
            continue
        keywords.append(str(item).strip())
        keywords.extend(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", str(item)))
    deduped = []
    for keyword in keywords:
        if keyword and keyword not in deduped:
            deduped.append(keyword)
    return deduped


def _candidate_from_chunk(chunk):
    return {
        "chunk_id": chunk.id,
        "section_title": chunk.section_title,
        "page": chunk.page_start,
        "location": _location(chunk),
        "paragraph_ids": chunk.paragraph_ids or [],
        "text": chunk.text,
    }


def _location(chunk):
    section = chunk.section_title or "未知章节"
    if chunk.page_start and chunk.page_end and chunk.page_start != chunk.page_end:
        return "%s，第%s-%s页" % (section, chunk.page_start, chunk.page_end)
    if chunk.page_start:
        return "%s，第%s页" % (section, chunk.page_start)
    return section

