"""Word 批注解析（设计§3.2）。

批注不在主文档里：内容在 `word/comments.xml`，锚点（commentRangeStart/End）散落在 `word/document.xml`。
python-docx 对批注支持弱，故**直接解析 XML**：读批注内容 + 在 document.xml 按文档顺序定位每条批注锚定的
原文片段与所在章节（最近上文标题，启发式）。产物供 §5 编译成评分项要求/扣分规则。

线程批注/已解析状态（commentsExtended.xml）为可选增强，暂不解析。
"""

import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

HEADING_RE = re.compile(r"^(第[一二三四五六七八九十\d]+[章节]|[\d]+(\.\d+)*\s|摘要|引言|绪论|结论|总结|参考文献|致谢)")


def _q(tag):
    return "{%s}%s" % (W, tag)


def parse_comments(docx_bytes):
    """返回 [{comment_id, author, comment_text, anchor_text, section_title}]；无批注或解析失败 → []。"""
    try:
        with zipfile.ZipFile(BytesIO(bytes(docx_bytes))) as archive:
            names = archive.namelist()
            if "word/comments.xml" not in names:
                return []
            comments_xml = archive.read("word/comments.xml")
            document_xml = archive.read("word/document.xml") if "word/document.xml" in names else b""
    except Exception:
        return []

    comments = _comment_texts(comments_xml)
    anchors = _anchors(document_xml)
    result = []
    for comment_id, meta in comments.items():
        anchor = anchors.get(comment_id, {})
        result.append(
            {
                "comment_id": comment_id,
                "author": meta.get("author"),
                "comment_text": meta.get("text", ""),
                "anchor_text": anchor.get("anchor_text", ""),
                "section_title": anchor.get("section_title", ""),
            }
        )
    return result


def _comment_texts(comments_xml):
    try:
        root = ET.fromstring(comments_xml)
    except ET.ParseError:
        return {}
    out = {}
    for comment in root.findall(_q("comment")):
        comment_id = comment.get(_q("id"))
        if comment_id is None:
            continue
        text = "".join(node.text or "" for node in comment.iter(_q("t"))).strip()
        out[comment_id] = {"author": comment.get(_q("author")), "text": text}
    return out


def _anchors(document_xml):
    if not document_xml:
        return {}
    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError:
        return {}

    open_ids = set()
    accumulated = {}
    section_at = {}
    current_section = ""

    for paragraph in root.iter(_q("p")):
        para_text = "".join(node.text or "" for node in paragraph.iter(_q("t"))).strip()
        for element in paragraph.iter():
            tag = element.tag
            if tag == _q("commentRangeStart"):
                comment_id = element.get(_q("id"))
                if comment_id is not None:
                    open_ids.add(comment_id)
                    accumulated.setdefault(comment_id, [])
                    section_at.setdefault(comment_id, current_section)
            elif tag == _q("commentRangeEnd"):
                open_ids.discard(element.get(_q("id")))
            elif tag == _q("t") and open_ids:
                for comment_id in open_ids:
                    accumulated[comment_id].append(element.text or "")
        if para_text and _is_heading(paragraph, para_text):
            current_section = para_text

    return {
        comment_id: {"anchor_text": "".join(parts).strip(), "section_title": section_at.get(comment_id, "")}
        for comment_id, parts in accumulated.items()
    }


def _is_heading(paragraph, text):
    ppr = paragraph.find(_q("pPr"))
    if ppr is not None:
        pstyle = ppr.find(_q("pStyle"))
        if pstyle is not None:
            val = pstyle.get(_q("val")) or ""
            if "Heading" in val or "Title" in val or "标题" in val:
                return True
    return bool(len(text) <= 40 and HEADING_RE.match(text))
