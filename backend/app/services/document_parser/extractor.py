"""Profile-neutral DOCX/PDF layout extraction.

This layer records source text and layout facts only. It deliberately knows
nothing about theses, students, references, required sections, or scoring.
Business profiles interpret the resulting immutable projection separately.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    page: int
    ordinal: int
    kind: str
    text: str
    style_name: str = ""
    max_font_size: float | None = None
    all_bold: bool | None = None
    table_index: int | None = None
    row_index: int | None = None
    cell_texts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    schema_version: str
    media_type: str
    source_suffix: str
    blocks: tuple[ExtractedBlock, ...]
    heading_candidates: tuple[str, ...]


def extract_document(file_path) -> ExtractedDocument:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        blocks, headings = _extract_docx(path)
        media_type = (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
    elif suffix == ".pdf":
        blocks, headings = _extract_pdf(path)
        media_type = "application/pdf"
    else:
        raise ValueError(
            "unsupported file type; only .docx and text PDF are supported"
        )
    return ExtractedDocument(
        schema_version="extracted-document@1",
        media_type=media_type,
        source_suffix=suffix,
        blocks=tuple(blocks),
        heading_candidates=tuple(sorted(headings)),
    )


def _extract_docx(path: Path):
    from docx import Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    blocks = []
    headings = set()
    ordinal = 0
    table_index = 0
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = getattr(paragraph.style, "name", "") or ""
            blocks.append(
                ExtractedBlock(
                    page=1,
                    ordinal=ordinal,
                    kind="paragraph",
                    text=text,
                    style_name=style_name,
                )
            )
            ordinal += 1
            if (
                "Heading" in style_name
                or "Title" in style_name
                or "标题" in style_name
            ):
                headings.add(text)
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            for row_index, row in enumerate(table.rows):
                # Merged DOCX cells are repeated by python-docx; retain each
                # underlying XML cell once so stable row text is not duplicated.
                seen_cells = set()
                cells = []
                for cell in row.cells:
                    cell_identity = id(cell._tc)
                    if cell_identity in seen_cells:
                        continue
                    seen_cells.add(cell_identity)
                    text = cell.text.strip()
                    if text:
                        cells.append(text)
                if cells:
                    blocks.append(
                        ExtractedBlock(
                            page=1,
                            ordinal=ordinal,
                            kind="table_row",
                            text=" | ".join(cells),
                            table_index=table_index,
                            row_index=row_index,
                            cell_texts=tuple(cells),
                        )
                    )
                    ordinal += 1
            table_index += 1
    return blocks, headings


def _extract_pdf(path: Path):
    import fitz

    blocks = []
    span_sizes = []
    heading_inputs = []
    ordinal = 0
    with fitz.open(str(path)) as document:
        for page_index, page in enumerate(document, start=1):
            for raw_block in page.get_text("dict").get("blocks", []):
                if raw_block.get("type") != 0:
                    continue
                lines_text = []
                block_max_size = 0.0
                all_bold = True
                has_span = False
                for line in raw_block.get("lines", []):
                    parts = []
                    for span in line.get("spans", []):
                        span_text = span.get("text", "")
                        if not span_text.strip():
                            continue
                        has_span = True
                        parts.append(span_text)
                        size = float(span.get("size", 0) or 0)
                        span_sizes.append(size)
                        block_max_size = max(block_max_size, size)
                        if not (int(span.get("flags", 0) or 0) & 16):
                            all_bold = False
                    if parts:
                        lines_text.append("".join(parts))
                if not has_span:
                    continue
                text = " ".join(lines_text).strip()
                if not text:
                    continue
                blocks.append(
                    ExtractedBlock(
                        page=page_index,
                        ordinal=ordinal,
                        kind="text_block",
                        text=text,
                        max_font_size=block_max_size,
                        all_bold=all_bold,
                    )
                )
                ordinal += 1
                heading_inputs.append((text, block_max_size, all_bold))
    return blocks, _pdf_heading_texts(heading_inputs, span_sizes)


def _pdf_heading_texts(blocks, span_sizes):
    if not span_sizes:
        return set()
    body_size = _mode_size(span_sizes)
    headings = set()
    for text, max_size, all_bold in blocks:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or len(normalized) > 40:
            continue
        if normalized[-1] in "。．.；;，,、":
            continue
        if (body_size and max_size >= body_size * 1.15) or all_bold:
            headings.add(normalized)
    return headings


def _mode_size(sizes):
    rounded = [round(size * 2) / 2 for size in sizes if size > 0]
    if not rounded:
        return 0.0
    return Counter(rounded).most_common(1)[0][0]


__all__ = ["ExtractedBlock", "ExtractedDocument", "extract_document"]
