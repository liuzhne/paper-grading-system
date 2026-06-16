"""篇章一致性检查 · 确定性部分（设计§8.1）。

只做"代码能对的结构核验"，不做语义判断：
- 图/表引用双向核对：引用了但无题注、有题注但正文未引用、编号不连续；
- 编号制引文-参考文献双向核对：引用了但无对应条目、条目从未被引用、编号不连续；
- 著者-出版年制：编号制确定性核对不适用，仅产出 info 提示转人工/混合（语义部分见§8 的模型核验，后续做）。

产物是结构化"一致性发现"列表，进解析结果与报告；是否据此扣分由评分项/人工决定（人在回路）。
"""

import re

from backend.app.services.coherence.terminology import analyze_terminology

AUTHOR_YEAR_RE = re.compile(r"[（(][^）)]{1,40}[，,]\s*(19|20)\d{2}[）)]")
INTEXT_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


def analyze_coherence(full_text, references):
    findings = []
    findings.extend(_reference_integrity(full_text or "", "图"))
    findings.extend(_reference_integrity(full_text or "", "表"))
    findings.extend(_citation_integrity(full_text or "", references or []))
    findings.extend(analyze_terminology(full_text or ""))
    return findings


def _reference_integrity(full_text, kind):
    """图/表：题注（声明）↔ 正文引用 的双向核对。"""
    caption_re = re.compile(r"^%s\s*(\d{1,3})" % kind)
    ref_re = re.compile(r"%s\s*(\d{1,3})" % kind)
    declared = set()
    body_refs = set()
    for line in full_text.split("\n"):
        text = line.strip()
        caption = caption_re.match(text)
        if caption and len(text) <= 60:
            declared.add(int(caption.group(1)))  # 以"图N …"短行作为题注（声明）
        else:
            for match in ref_re.finditer(text):
                body_refs.add(int(match.group(1)))

    findings = []
    for number in sorted(body_refs - declared):
        findings.append(
            _finding("figure_ref_missing", "warning", "正文引用%s%d，但未找到对应的%s题注" % (kind, number, kind), refs=[number])
        )
    for number in sorted(declared - body_refs):
        findings.append(
            _finding("figure_unreferenced", "warning", "%s%d 有题注但正文未引用" % (kind, number), refs=[number])
        )
    for number in _numbering_gaps(declared):
        findings.append(
            _finding("figure_numbering_gap", "warning", "%s编号不连续：缺%s%d" % (kind, kind, number), refs=[number])
        )
    return findings


def _citation_integrity(full_text, references):
    """编号制引文 ↔ 参考文献条目 的双向核对。著者-年制只提示，不做确定性判断。"""
    # 正文引用扫描需剔除参考文献条目本身，否则条目里的 [n] 标号会被误当成正文引用，造成"自洽"假象。
    body_text = full_text
    for entry in references:
        if entry:
            body_text = body_text.replace(str(entry), " ")
    intext = {int(number) for number in INTEXT_CITATION_RE.findall(body_text)}
    ref_numbers = set()
    for entry in references:
        match = re.match(r"\s*\[(\d{1,3})\]", str(entry))
        if match:
            ref_numbers.add(int(match.group(1)))
    if not ref_numbers and references:
        ref_numbers = set(range(1, len(references) + 1))  # 条目无 [n] 前缀时按顺序编号兜底

    findings = []
    for number in sorted(intext - ref_numbers):
        findings.append(
            _finding("citation_missing_entry", "warning", "文中引用[%d]无对应参考文献条目" % number, refs=[number])
        )
    for number in sorted(ref_numbers - intext):
        findings.append(
            _finding("reference_uncited", "info", "参考文献[%d]未被正文引用" % number, refs=[number])
        )
    for number in _numbering_gaps(ref_numbers):
        findings.append(
            _finding("reference_numbering_gap", "warning", "参考文献编号不连续：缺[%d]" % number, refs=[number])
        )
    if AUTHOR_YEAR_RE.search(full_text) and len(intext) <= 1:
        findings.append(
            _finding(
                "citation_author_year",
                "info",
                "检测到著者-出版年制引用，编号制确定性核对不适用，建议人工/混合核验",
            )
        )
    return findings


def _numbering_gaps(numbers):
    if not numbers:
        return []
    return [number for number in range(1, max(numbers) + 1) if number not in numbers]


def _finding(kind, severity, message, refs=None, location=None):
    return {"kind": kind, "severity": severity, "message": message, "refs": refs or [], "location": location}
