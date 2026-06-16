import zipfile
from io import BytesIO

from backend.app.services.document_parser.track_changes import parse_track_changes
from backend.app.services.document_parser.track_changes import track_changes_findings

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_with_revisions():
    """构造仅含 word/document.xml 的最小 docx（parse_track_changes 只读该条目）。"""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="%s"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>第三章 研究方法</w:t></w:r></w:p>'
        '<w:p>'
        '<w:ins w:author="导师"><w:r><w:t>补充的研究方法说明</w:t></w:r></w:ins>'
        '<w:del w:author="学生"><w:r><w:delText>删去的旧表述</w:delText></w:r></w:del>'
        '</w:p>'
        '</w:body></w:document>'
    ) % W
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def test_parse_track_changes_extracts_ins_del_author_and_section():
    revisions = parse_track_changes(_docx_with_revisions())
    assert len(revisions) == 2
    insertion = next(r for r in revisions if r["type"] == "insertion")
    deletion = next(r for r in revisions if r["type"] == "deletion")
    assert insertion["text"] == "补充的研究方法说明"
    assert insertion["author"] == "导师"
    assert insertion["section_title"] == "第三章 研究方法"
    assert deletion["text"] == "删去的旧表述"
    assert deletion["author"] == "学生"


def test_track_changes_findings_aggregates_warning():
    findings = track_changes_findings(parse_track_changes(_docx_with_revisions()))
    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "track_changes"
    assert finding["severity"] == "warning"
    assert "1 处插入" in finding["message"]
    assert "1 处删除" in finding["message"]
    assert finding["refs"]


def test_no_revisions_returns_empty():
    document_xml = (
        '<w:document xmlns:w="%s"><w:body>'
        '<w:p><w:r><w:t>没有任何修订痕迹的正文</w:t></w:r></w:p>'
        '</w:body></w:document>'
    ) % W
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    revisions = parse_track_changes(buffer.getvalue())
    assert revisions == []
    assert track_changes_findings(revisions) == []


def test_malformed_bytes_safe():
    assert parse_track_changes(b"not a zip") == []
