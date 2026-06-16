"""修订痕迹（track changes）解析 —— 设计§14 通用化的确定性子集。

Word 修订痕迹存在 `word/document.xml`：插入 = `w:ins`（内含 `w:r/w:t`），删除 = `w:del`
（内含 `w:r/w:delText`）。提交终稿若仍残留**未接受/未拒绝**的修订，通常意味着定稿未完成或导师
改动残留——这是**确定性**可检的，作为篇章质量发现（warning）入报告，**不自动扣分**（人在回路）。

通用文件扩展（任意格式的 `DocumentParser` 接口化，§14）按设计§5.5 继续后置；此处只做 docx 修订痕迹
这一确定性切片，复用 `docx_comments` 的 zip+XML 解析与标题启发式。
"""

import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

HEADING_RE = re.compile(r"^(第[一二三四五六七八九十\d]+[章节]|[\d]+(\.\d+)*\s|摘要|引言|绪论|结论|总结|参考文献|致谢)")

_SNIPPET_LIMIT = 40
_REFS_LIMIT = 8


def _q(tag):
    return "{%s}%s" % (W, tag)


def parse_track_changes(docx_bytes):
    """返回 [{type: 'insertion'|'deletion', author, text, section_title}]；无修订或解析失败 → []。

    按文档顺序遍历，每条修订归到其最近上文标题（启发式章节定位，与批注解析一致）。
    """
    try:
        with zipfile.ZipFile(BytesIO(bytes(docx_bytes))) as archive:
            if "word/document.xml" not in archive.namelist():
                return []
            document_xml = archive.read("word/document.xml")
    except Exception:
        return []

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError:
        return []

    revisions = []
    current_section = ""
    for paragraph in root.iter(_q("p")):
        for element in paragraph.iter():
            if element.tag == _q("ins"):
                text = _collect_text(element, _q("t"))
                if text:
                    revisions.append(_revision("insertion", element, text, current_section))
            elif element.tag == _q("del"):
                text = _collect_text(element, _q("delText"))
                if text:
                    revisions.append(_revision("deletion", element, text, current_section))
        para_text = "".join(node.text or "" for node in paragraph.iter(_q("t"))).strip()
        if para_text and _is_heading(paragraph, para_text):
            current_section = para_text
    return revisions


def track_changes_findings(revisions):
    """把修订痕迹汇总成一条篇章质量发现（warning）；无修订 → []。不自动扣分。"""
    if not revisions:
        return []
    insertions = sum(1 for r in revisions if r["type"] == "insertion")
    deletions = len(revisions) - insertions
    authors = sorted({r["author"] for r in revisions if r.get("author")})
    refs = []
    for rev in revisions[:_REFS_LIMIT]:
        label = "插入" if rev["type"] == "insertion" else "删除"
        where = ("〔%s〕" % rev["section_title"]) if rev["section_title"] else ""
        refs.append("%s%s：%s" % (label, where, rev["text"]))
    author_note = ("（修订者：%s）" % "、".join(authors)) if authors else ""
    message = (
        "文档含 %d 处未接受的修订痕迹（%d 处插入、%d 处删除）%s；"
        "提交定稿前应接受或拒绝所有修订。" % (len(revisions), insertions, deletions, author_note)
    )
    return [
        {
            "kind": "track_changes",
            "severity": "warning",
            "message": message,
            "refs": refs,
            "location": None,
        }
    ]


def _revision(kind, element, text, section_title):
    return {
        "type": kind,
        "author": element.get(_q("author")),
        "text": _clip(text),
        "section_title": section_title,
    }


def _collect_text(element, text_tag):
    return "".join(node.text or "" for node in element.iter(text_tag)).strip()


def _clip(text):
    text = " ".join(text.split())
    return text if len(text) <= _SNIPPET_LIMIT else text[:_SNIPPET_LIMIT] + "…"


def _is_heading(paragraph, text):
    ppr = paragraph.find(_q("pPr"))
    if ppr is not None:
        pstyle = ppr.find(_q("pStyle"))
        if pstyle is not None:
            val = pstyle.get(_q("val")) or ""
            if "Heading" in val or "Title" in val or "标题" in val:
                return True
    return bool(len(text) <= 40 and HEADING_RE.match(text))
