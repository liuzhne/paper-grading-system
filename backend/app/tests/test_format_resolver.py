from io import BytesIO

from docx import Document
from docx.shared import Pt

from backend.app.services.document_parser.format_resolver import _resolve
from backend.app.services.document_parser.format_resolver import resolve_default_format

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def _styles(doc_defaults="", normal=""):
    return ("<w:styles %s>%s%s</w:styles>" % (W, doc_defaults, normal)).encode("utf-8")


def test_resolve_from_doc_defaults():
    styles = _styles(
        doc_defaults=(
            "<w:docDefaults>"
            '<w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
            "</w:docDefaults>"
        )
    )
    spec = _resolve(styles, None)
    assert spec["body_font_ascii"] == "Times New Roman"
    assert spec["body_font_east_asian"] == "宋体"
    assert spec["body_font_size_pt"] == 12.0  # 24 半磅
    assert spec["line_spacing"] == 1.5  # 360/240


def test_normal_style_overrides_doc_defaults():
    styles = _styles(
        doc_defaults='<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="A"/><w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults>',
        normal='<w:style w:styleId="Normal"><w:rPr><w:rFonts w:ascii="Heiti"/><w:sz w:val="28"/></w:rPr></w:style>',
    )
    spec = _resolve(styles, None)
    assert spec["body_font_ascii"] == "Heiti"
    assert spec["body_font_size_pt"] == 14.0


def test_unknown_when_nothing_declared():
    spec = _resolve(_styles(), None)
    assert spec["body_font_ascii"] is None
    assert spec["body_font_east_asian"] is None
    assert spec["body_font_size_pt"] is None
    assert spec["line_spacing"] is None


def test_theme_fallback_fonts():
    theme = (
        "<a:theme %s><a:themeElements><a:fontScheme><a:minorFont>"
        '<a:latin typeface="Calibri"/><a:font script="Hans" typeface="等线"/>'
        "</a:minorFont></a:fontScheme></a:themeElements></a:theme>" % A
    ).encode("utf-8")
    spec = _resolve(_styles(), theme)
    assert spec["body_font_ascii"] == "Calibri"
    assert spec["body_font_east_asian"] == "等线"
    assert spec["body_font_size_pt"] is None  # theme 不定义字号


def test_exact_line_rule_is_not_a_multiple():
    styles = _styles(
        doc_defaults='<w:docDefaults><w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="exact"/></w:pPr></w:pPrDefault></w:docDefaults>'
    )
    assert _resolve(styles, None)["line_spacing"] is None


def test_resolve_default_format_from_real_docx():
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.5
    buffer = BytesIO()
    doc.save(buffer)

    spec = resolve_default_format(buffer.getvalue())
    assert spec["body_font_ascii"] == "Times New Roman"
    assert spec["body_font_size_pt"] == 12.0
    assert spec["line_spacing"] == 1.5
