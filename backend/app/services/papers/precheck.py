"""上传后、评分前的解析预检（前端 v2 计划 §5-E）。

**只汇总既有解析诊断。** 不重新传文件，也不额外跑一次全文解析——否则一次
「预检」会把整批材料重解析一遍，既慢，又可能得出与后续评分实际使用的解析
结果不一致的结论。

扫描件按决策 1 明确拒绝：系统不支持 OCR。文案要给出下一步（提供文字版），
而不是像设计稿那样承诺一个并不存在的「将走 OCR 流程」。
"""

from __future__ import annotations


#: 解析器对扫描件的报错特征。见 document_parser/parser.py。
_SCANNED_MARKERS = ("scanned PDFs require OCR", "no readable text found")

#: 低于此解析质量给出提醒，但不阻断——材料仍可评分，只是复核时要多留意。
LOW_QUALITY_THRESHOLD = 0.5

MAX_PAPERS = 500


def _finding(paper, code, severity, message):
    return {
        "paper_id": paper.id,
        "student_id": paper.student_id,
        "file_name": paper.file_name,
        "code": code,
        "severity": severity,
        "message": message,
    }


def _inspect(paper):
    if paper.status == "failed":
        error = paper.error_message or ""
        if any(marker in error for marker in _SCANNED_MARKERS):
            return _finding(
                paper,
                "scanned_document",
                "blocking",
                "该文件是扫描件，系统不支持 OCR，无法提取正文。"
                "请提供文字版 PDF 或 DOCX 后重新上传。",
            )
        return _finding(
            paper,
            "parse_failed",
            "blocking",
            "解析失败：%s" % (paper.error_message or "未记录具体原因"),
        )

    if paper.status != "parsed":
        # 「尚未解析」与「解析出来是空的」是两回事，不能混为一谈。
        return _finding(
            paper,
            "not_parsed",
            "blocking",
            "该材料尚未完成解析（当前状态：%s），请先解析再开始评分。"
            % (paper.status,),
        )

    quality = paper.parse_quality
    if quality is not None and float(quality) < LOW_QUALITY_THRESHOLD:
        return _finding(
            paper,
            "low_parse_quality",
            "warning",
            "解析质量偏低（%.2f），正文可能不完整；评分结果建议重点复核。"
            % float(quality),
        )
    return None


def build_precheck(papers):
    findings = []
    ready = 0
    for paper in papers:
        finding = _inspect(paper)
        if finding is None:
            ready += 1
            continue
        findings.append(finding)
        if finding["severity"] != "blocking":
            ready += 1

    blocking = [f for f in findings if f["severity"] == "blocking"]
    return {
        "total": len(papers),
        "ready_count": ready,
        "blocking_count": len(blocking),
        "warning_count": len(findings) - len(blocking),
        # 有阻断项时不允许开始评分：解析失败的材料评出来的分没有意义。
        "can_start": not blocking and bool(papers),
        "findings": findings,
    }


__all__ = ["build_precheck", "LOW_QUALITY_THRESHOLD", "MAX_PAPERS"]
