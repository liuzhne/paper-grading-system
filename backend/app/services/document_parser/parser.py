import re
from pathlib import Path

from backend.app.services.document_parser.types import ParsedPaper
from backend.app.services.document_parser.types import ParsedParagraph
from backend.app.services.document_parser.types import ParsedSection
from backend.app.services.document_parser.types import StructureCheck
from backend.app.services.coherence import analyze_coherence
from backend.app.services.document_parser.track_changes import parse_track_changes
from backend.app.services.document_parser.track_changes import track_changes_findings
from backend.app.services.document_parser.extractor import ExtractedDocument
from backend.app.services.document_parser.extractor import extract_document

SECTION_RE = re.compile(
    r"^("
    r"摘要|中文摘要|英文摘要|Abstract|ABSTRACT|关键词|Key\s*words?|目录|结论|总结|参考文献|致谢|"
    r"第[一二三四五六七八九十百\d]+[章节篇].*|"
    r"\d+(\.\d+)*[、.\s].{1,80}"
    r")$"
)
STUDENT_ID_RE = re.compile(r"(学生\s*学号|学号|Student\s*ID)[:： \t]*([A-Za-z0-9\-]{5,30})", re.IGNORECASE)
STUDENT_NAME_RE = re.compile(
    r"(学生\s*姓名|姓名|作者|Name)[:： \t]*([\u4e00-\u9fffA-Za-z·]{2,20}(?:[ \t]+[\u4e00-\u9fffA-Za-z·]{1,20})?)"
)
COVER_TITLE_RE = re.compile(r"论\s*文\s*题\s*目\s*[:：]\s*(.*)")
COVER_FIELD_RE = re.compile(
    r"^(?P<label>"
    r"论\s*文\s*题\s*目|"
    r"学\s*院|院\s*系|所在\s*学院|"
    r"专\s*业|"
    r"学生\s*姓名|姓\s*名|"
    r"学生\s*学\s*号|学\s*号|"
    r"指导\s*教师|指导\s*老师|导\s*师"
    r")\s*[:：]\s*(?P<value>.*)$"
)
COVER_STOP_FIELD_RE = re.compile(
    r"^(学\s*院|院\s*系|专\s*业|学生\s*姓名|姓\s*名|学生\s*学\s*号|学\s*号|指导\s*教师|指导\s*老师|导\s*师|导师\s*职称|完成\s*日期)\s*[:：]?"
)
INSTITUTION_RE = re.compile(r"大连\s*东软\s*信息\s*学院|Dalian\s+Neusoft\s+University", re.IGNORECASE)
TITLE_SKIP_KEYWORDS = (
    "大连东软信息学院",
    "Dalian Neusoft University",
    "毕业设计",
    "论文题目",
    "学院：",
    "专业：",
    "学生姓名",
    "学生学号",
    "指导教师",
    "导师职称",
    "完成日期",
)


def parse_document(file_path):
    path = Path(file_path)
    return interpret_thesis_document(extract_document(path), source_path=path)


def interpret_thesis_document(extracted, *, source_path=None):
    """Interpret profile-neutral extracted layout with thesis semantics."""

    if not isinstance(extracted, ExtractedDocument):
        raise TypeError("thesis interpreter requires an ExtractedDocument")
    if extracted.schema_version != "extracted-document@1":
        raise ValueError("unsupported extracted document schema")

    paragraphs = [
        (block.page, _normalize_text(block.text))
        for block in extracted.blocks
        if _normalize_text(block.text)
    ]
    if not paragraphs:
        raise ValueError("no readable text found; scanned PDFs require OCR and are not supported in MVP")
    heading_texts = {
        _normalize_text(text)
        for text in extracted.heading_candidates
        if _normalize_text(text)
    }

    sections = _build_sections(paragraphs, heading_texts)
    full_text = "\n".join(text for _, text in paragraphs)
    cover_metadata = _extract_cover_metadata(paragraphs)
    references = _extract_references(sections)
    coherence_findings = analyze_coherence(full_text, references)
    if extracted.source_suffix == ".docx" and source_path is not None:
        coherence_findings = coherence_findings + _track_changes_findings(
            Path(source_path)
        )
    checks = _structure_checks(full_text, sections, references)
    structure_confidence = _estimate_structure_confidence(sections, heading_texts)
    checks.append(
        StructureCheck(
            code="SECTION_DETECTION",
            name="章节识别可靠性",
            passed=structure_confidence >= 0.5,
            message="章节识别置信度 %.2f（依据：%s）" % (structure_confidence, "标题样式+规则" if heading_texts else "规则启发式"),
            location=None if structure_confidence >= 0.5 else "章节划分不可靠，建议人工确认",
        )
    )
    parse_quality = _estimate_parse_quality(full_text, checks, sections)

    return ParsedPaper(
        structure_confidence=structure_confidence,
        coherence_findings=coherence_findings,
        title=cover_metadata.get("title") or _infer_title(paragraphs),
        student_id=cover_metadata.get("student_id") or _first_regex_group(STUDENT_ID_RE, full_text),
        student_name=cover_metadata.get("student_name") or _first_regex_group(STUDENT_NAME_RE, full_text),
        institution=cover_metadata.get("institution"),
        department=cover_metadata.get("department"),
        major=cover_metadata.get("major"),
        advisor=cover_metadata.get("advisor"),
        sections=sections,
        references=references,
        full_text=full_text,
        structure_checks=checks,
        parse_quality=parse_quality,
    )


def _extract_docx(path):
    extracted = extract_document(path)
    return (
        [(block.page, block.text) for block in extracted.blocks],
        set(extracted.heading_candidates),
    )


def _extract_pdf(path):
    extracted = extract_document(path)
    return (
        [(block.page, block.text) for block in extracted.blocks],
        set(extracted.heading_candidates),
    )


def _normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def _build_sections(paragraphs, heading_texts=None):
    heading_texts = heading_texts or set()
    sections = []
    current = None
    paragraph_number = 1

    for page, text in paragraphs:
        # 多策略：标题样式（heading_texts）∪ 规则启发式（编号/已知章节名）。
        if text in heading_texts or _looks_like_section_title(text):
            if current is not None:
                current.page_end = page
            current = ParsedSection(
                section_id="sec_%03d" % (len(sections) + 1),
                title=text[:120],
                level=_infer_level(text),
                page_start=page,
                page_end=page,
                paragraphs=[],
            )
            sections.append(current)
            continue

        if current is None:
            current = ParsedSection(
                section_id="sec_001",
                title="未命名开头",
                level=1,
                page_start=page,
                page_end=page,
                paragraphs=[],
            )
            sections.append(current)

        current.page_end = page
        current.paragraphs.append(
            ParsedParagraph(
                paragraph_id="p_%04d" % paragraph_number,
                page=page,
                text=text,
            )
        )
        paragraph_number += 1

    return sections


def _looks_like_section_title(text):
    if len(text) > 90:
        return False
    return bool(SECTION_RE.match(text.strip()))


def _infer_level(text):
    if re.match(r"^\d+\.\d+", text):
        return 2
    return 1


def _infer_title(paragraphs):
    for _, text in paragraphs[:20]:
        inline_field = _match_cover_field(text)
        if inline_field and inline_field[0] == "title":
            return inline_field[1][:200]
        if (
            len(text) >= 4
            and not _looks_like_section_title(text)
            and not STUDENT_ID_RE.search(text)
            and not _is_title_skip_line(text)
            and not _match_cover_field(text)
        ):
            return text[:200]
    return None


def _extract_cover_metadata(paragraphs):
    lines = [text for _, text in paragraphs[:120]]
    metadata = {
        "title": None,
        "institution": None,
        "department": None,
        "major": None,
        "student_name": None,
        "student_id": None,
        "advisor": None,
    }

    for line in lines[:60]:
        if metadata["institution"] is None and INSTITUTION_RE.search(line):
            metadata["institution"] = _normalize_institution(line)

    metadata["title"] = _extract_cover_title(lines)

    for line in lines:
        field = _match_cover_field(line)
        if not field:
            continue
        name, value = field
        if name in metadata and value and metadata[name] is None:
            metadata[name] = value

    return metadata


def _extract_cover_title(lines):
    for index, line in enumerate(lines[:60]):
        match = COVER_TITLE_RE.search(line)
        if not match:
            continue

        title_parts = []
        first_part = _clean_field_value(match.group(1))
        if first_part:
            title_parts.append(first_part)

        for next_line in lines[index + 1 : index + 5]:
            candidate = _clean_field_value(next_line)
            if not _looks_like_title_continuation(candidate):
                break
            title_parts.append(candidate)

        title = _join_title_parts(title_parts)
        if title:
            return title[:200]
    return None


def _match_cover_field(text):
    inline_match = COVER_FIELD_RE.match(text.strip())
    if inline_match:
        field_name = _field_name_from_label(inline_match.group("label"))
        value = _clean_field_value(inline_match.group("value"))
        if value:
            return field_name, value

    if "|" not in text:
        return None

    cells = [_clean_field_value(cell) for cell in text.split("|")]
    cells = [cell for cell in cells if cell]
    for index, cell in enumerate(cells):
        field_name = _field_name_from_label(cell)
        if not field_name:
            continue
        inline_value = _value_after_label(cell)
        value_parts = []
        if inline_value:
            value_parts.append(inline_value)
        value_parts.extend(cells[index + 1 :])
        value = _clean_field_value(" ".join(value_parts))
        if value:
            return field_name, value
    return None


def _field_name_from_label(label):
    compact = _compact_text(label)
    if compact.startswith("论文题目"):
        return "title"
    if compact in {"学院", "院系", "所在学院"}:
        return "department"
    if compact == "专业":
        return "major"
    if compact in {"学生姓名", "姓名"}:
        return "student_name"
    if compact in {"学生学号", "学号"}:
        return "student_id"
    if compact in {"指导教师", "指导老师", "导师"}:
        return "advisor"
    return None


def _value_after_label(text):
    match = COVER_FIELD_RE.match(text.strip())
    if not match:
        return None
    return _clean_field_value(match.group("value"))


def _clean_field_value(value):
    value = re.sub(r"^[\s|:：_＿\-—]+", "", value or "")
    value = re.sub(r"[\s|:：_＿\-—]+$", "", value)
    value = re.sub(r"[＿_]{2,}", " ", value)
    value = re.sub(r"[—-]{2,}", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _compact_text(text):
    return re.sub(r"[\s:：|_＿\-—]+", "", text or "")


def _normalize_institution(text):
    if re.search(r"大连\s*东软\s*信息\s*学院", text):
        return "大连东软信息学院"
    if re.search(r"Dalian\s+Neusoft\s+University", text, re.IGNORECASE):
        return "Dalian Neusoft University of Information"
    return _clean_field_value(text)


def _looks_like_title_continuation(text):
    if not text or len(text) > 120:
        return False
    if "|" in text or _looks_like_section_title(text):
        return False
    if COVER_STOP_FIELD_RE.match(text) or INSTITUTION_RE.search(text):
        return False
    if _is_title_skip_line(text):
        return False
    return True


def _join_title_parts(parts):
    title = " ".join(part for part in parts if part)
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", title)
    return title or None


def _is_title_skip_line(text):
    compact = _compact_text(text)
    if not compact:
        return True
    if any(keyword in text for keyword in TITLE_SKIP_KEYWORDS):
        return True
    if any(_compact_text(keyword) in compact for keyword in TITLE_SKIP_KEYWORDS):
        return True
    return False


def _first_regex_group(pattern, text):
    match = pattern.search(text)
    if not match:
        return None
    return match.group(2).strip()


def _track_changes_findings(path):
    """读 docx 修订痕迹 → 篇章质量发现（best-effort，失败不影响解析主流程）。"""
    try:
        revisions = parse_track_changes(path.read_bytes())
    except Exception:
        return []
    return track_changes_findings(revisions)


def _extract_references(sections):
    references = []
    in_references = False
    for section in sections:
        if "参考文献" in section.title:
            in_references = True
        if in_references:
            for paragraph in section.paragraphs:
                if len(paragraph.text) > 6:
                    references.append(paragraph.text)
    return references


def _structure_checks(full_text, sections, references):
    checks = [
        _contains_check("HAS_ABSTRACT_CN", "中文摘要", full_text, ["中文摘要", "摘要"]),
        _contains_check("HAS_ABSTRACT_EN", "英文摘要", full_text, ["Abstract", "ABSTRACT", "英文摘要"]),
        _contains_check("HAS_KEYWORDS", "关键词", full_text, ["关键词", "Key words", "Keywords"]),
        _contains_check("HAS_TOC", "目录", full_text, ["目录"]),
        _contains_check("HAS_CONCLUSION", "结论", full_text, ["结论", "总结"]),
        StructureCheck(
            code="HAS_REFERENCES",
            name="参考文献",
            passed=bool(references) or "参考文献" in full_text,
            message="检测到参考文献" if references or "参考文献" in full_text else "未检测到参考文献",
            location="参考文献章节" if references else None,
        ),
        StructureCheck(
            code="WORD_COUNT_MIN",
            name="正文字数",
            passed=len(full_text) >= 3000,
            message="正文长度约 %d 字符" % len(full_text),
            location=None,
        ),
        StructureCheck(
            code="SECTION_COUNT_MIN",
            name="章节数量",
            passed=len(sections) >= 4,
            message="检测到 %d 个章节" % len(sections),
            location=None,
        ),
    ]
    return checks


def _contains_check(code, name, text, keywords):
    found = next((keyword for keyword in keywords if keyword in text), None)
    return StructureCheck(
        code=code,
        name=name,
        passed=found is not None,
        message="检测到%s" % name if found else "未检测到%s" % name,
        location=found,
    )


def _estimate_structure_confidence(sections, heading_texts):
    """章节识别置信度（N3）：标题样式可用是强信号；否则只靠规则启发式，
    叠加"章节数量"与"是否含摘要/结论/参考文献等预期章节"两项信号。低于阈值会告警人工确认。"""
    style_signal = 0.45 if heading_texts else 0.0  # 标题样式是分段可靠性的最强信号
    section_signal = min(len(sections), 6) / 6 * 0.3
    titles = " ".join(section.title for section in sections)
    expected_hits = sum(1 for keyword in ("摘要", "结论", "参考文献") if keyword in titles)
    expected_signal = expected_hits / 3 * 0.25
    return round(min(style_signal + section_signal + expected_signal, 1.0), 3)


def _estimate_parse_quality(full_text, checks, sections):
    if not full_text:
        return 0
    passed_ratio = sum(1 for check in checks if check.passed) / max(len(checks), 1)
    section_bonus = min(len(sections), 6) / 6
    length_bonus = min(len(full_text), 10000) / 10000
    quality = 0.45 * passed_ratio + 0.3 * section_bonus + 0.25 * length_bonus
    return round(max(0.05, min(quality, 1.0)), 3)
