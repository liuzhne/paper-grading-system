from io import BytesIO
from types import SimpleNamespace

from docx import Document
from docx.shared import Pt

from backend.app.services.document_parser.format_check import compare_format
from backend.app.services.scoring.engine import _compute_format_findings


def test_compare_flags_font_and_spacing_mismatch_but_not_matching_size():
    actual = {"body_font_ascii": "Calibri", "body_font_size_pt": 12.0, "line_spacing": 1.0}
    expected = {"body_font_ascii": "Times New Roman", "body_font_size_pt": 12.0, "line_spacing": 1.5}
    findings = compare_format(actual, expected)
    pairs = {(f["field"], f["kind"]) for f in findings}
    assert ("body_font_ascii", "format_mismatch") in pairs
    assert ("line_spacing", "format_mismatch") in pairs
    assert all(f["field"] != "body_font_size_pt" for f in findings)  # 字号一致 → 无发现


def test_compare_skips_fields_not_required_by_template():
    assert compare_format({"body_font_ascii": "X"}, {"body_font_ascii": None}) == []


def test_compare_unknown_actual_is_info_not_warning():
    findings = compare_format({"body_font_size_pt": None}, {"body_font_size_pt": 12})
    assert len(findings) == 1
    assert findings[0]["kind"] == "format_unknown"
    assert findings[0]["severity"] == "info"  # unknown 不判错


def test_compute_format_findings_for_docx(tmp_path):
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.0
    path = tmp_path / "s.docx"
    doc.save(str(path))

    paper = SimpleNamespace(file_path=str(path))
    rubric = SimpleNamespace(format_spec={"body_font_ascii": "宋体", "body_font_size_pt": 14, "line_spacing": 1.5, "source": "template"})
    fields = {f["field"] for f in _compute_format_findings(paper, rubric)}
    assert "body_font_ascii" in fields  # Times New Roman vs 宋体
    assert "body_font_size_pt" in fields  # 12 vs 14


def test_compute_format_findings_skips_non_docx():
    paper = SimpleNamespace(file_path="/papers/x.pdf")
    rubric = SimpleNamespace(format_spec={"body_font_ascii": "宋体", "source": "template"})
    assert _compute_format_findings(paper, rubric) == []


def test_compute_format_findings_skips_empty_spec():
    paper = SimpleNamespace(file_path="/papers/x.docx")
    rubric = SimpleNamespace(format_spec={"body_font_ascii": None, "line_spacing": None, "source": "template"})
    assert _compute_format_findings(paper, rubric) == []
