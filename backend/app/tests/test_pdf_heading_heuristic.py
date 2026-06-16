import fitz

from backend.app.services.document_parser.parser import parse_document


def _write_pdf(path, lines):
    """lines: [(text, fontsize, fontname)]，逐行竖排写入单页 PDF。"""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for text, size, fontname in lines:
        page.insert_text((72, y), text, fontsize=size, fontname=fontname)
        y += size + 12
    doc.save(str(path))
    doc.close()


def test_pdf_large_font_line_detected_as_heading(tmp_path):
    pdf = tmp_path / "thesis.pdf"
    _write_pdf(
        pdf,
        [
            ("Chapter 1 Introduction", 20, "helv"),  # 大字号 → 标题
            ("This is body text describing the background.", 10, "helv"),
            ("More ordinary body content for the section.", 10, "helv"),
        ],
    )
    parsed = parse_document(str(pdf))
    titles = [section.title for section in parsed.sections]
    assert any("Chapter 1 Introduction" in title for title in titles)


def test_pdf_uniform_font_yields_no_style_headings(tmp_path):
    pdf = tmp_path / "flat.pdf"
    _write_pdf(
        pdf,
        [
            ("Some opening sentence without heading style.", 11, "helv"),
            ("Another plain sentence at the same size here.", 11, "helv"),
        ],
    )
    # 字号一致且非编号标题 → 不应把任意行当标题（heading_texts 为空，退回规则启发式）。
    from backend.app.services.document_parser.parser import _extract_pdf
    from pathlib import Path

    _, heading_texts = _extract_pdf(Path(str(pdf)))
    assert heading_texts == set()
