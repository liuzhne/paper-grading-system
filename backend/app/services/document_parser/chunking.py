from backend.app.db.models import PaperChunk


def build_chunks(parsed_paper, paper_id, max_chars=1000, overlap_chars=120):
    chunks = []
    for section in parsed_paper.sections:
        buffer = []
        paragraph_ids = []
        page_start = None
        page_end = None

        for paragraph in section.paragraphs:
            if page_start is None:
                page_start = paragraph.page
            page_end = paragraph.page
            candidate = "\n".join(buffer + [paragraph.text])
            if len(candidate) > max_chars and buffer:
                chunks.append(
                    _make_chunk(paper_id, section.title, page_start, page_end, paragraph_ids, "\n".join(buffer))
                )
                overlap = "\n".join(buffer)[-overlap_chars:] if overlap_chars else ""
                buffer = [overlap, paragraph.text] if overlap else [paragraph.text]
                paragraph_ids = [paragraph.paragraph_id]
                page_start = paragraph.page
            else:
                buffer.append(paragraph.text)
                paragraph_ids.append(paragraph.paragraph_id)

        if buffer:
            chunks.append(_make_chunk(paper_id, section.title, page_start, page_end, paragraph_ids, "\n".join(buffer)))

    if not chunks and parsed_paper.full_text:
        chunks.append(_make_chunk(paper_id, "全文", 1, 1, [], parsed_paper.full_text[:max_chars]))
    return chunks


def _make_chunk(paper_id, section_title, page_start, page_end, paragraph_ids, text):
    return PaperChunk(
        paper_id=paper_id,
        section_title=section_title,
        page_start=page_start,
        page_end=page_end,
        paragraph_ids=paragraph_ids,
        text=text.strip(),
    )

