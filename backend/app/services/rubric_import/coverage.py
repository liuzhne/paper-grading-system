"""覆盖率计算（解析重构方案 §4.3）。

分母由文档角色决定，不由文件格式决定：
- rules：所有非空单元都要处理，``context`` 不算已处理；
- template：只统计批注与命中规则信号的段落，普通正文默认 context 不进分母；
- reference：不算覆盖率，只列出疑似规则。
"""

from __future__ import annotations

from backend.app.services.rubric_import.classification.signals import is_noise
from backend.app.services.rubric_import.classification.signals import rule_signals
from backend.app.services.rubric_import.sources.units import STATUSES
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit


def unit_signals(unit: SourceUnit, profile_terms=()) -> list[str]:
    if is_noise(unit.text):
        return []
    signals = list(rule_signals(unit.text, profile_terms=tuple(profile_terms)))
    if unit.kind == "comment":
        signals.insert(0, "comment")
    return signals


BLOCKING_SIGNALS = frozenset({"comment", "score", "verb"})


def is_blocking(signals) -> bool:
    """批注与带分值/扣分动词的单元视为疑似规则并阻断发布；只命中规范用语或
    Profile 领域词的单元只提示——否则几乎每份论文模板正文都会阻断发布。"""

    return bool(BLOCKING_SIGNALS.intersection(signals))


def compute_coverage(ledger: SourceLedger, *, profile_terms=()) -> dict:
    documents = []
    for doc_id, role in ledger.documents().items():
        units = ledger.units(doc_id=doc_id)
        counts = {status: 0 for status in STATUSES}
        for unit in units:
            counts[ledger.status(unit.unit_id).status] += 1
        if role == "rules":
            denominator = len(units) - counts["structural"] - counts["ignored_by_rule"]
            handled = counts["consumed"]
        elif role == "template":
            must = [u for u in units if unit_signals(u, profile_terms)]
            denominator = len(must)
            handled = sum(1 for u in must if ledger.status(u.unit_id).status != "unclaimed")
        else:
            denominator = handled = None
        ratio = None if denominator is None else (1.0 if denominator == 0 else handled / denominator)
        documents.append(
            {
                "doc_id": doc_id,
                "doc_role": role,
                "total": len(units),
                "counts": counts,
                "denominator": denominator,
                "handled": handled,
                "ratio": ratio,
            }
        )

    unclaimed = []
    for unit in ledger:
        if ledger.status(unit.unit_id).status != "unclaimed" or is_noise(unit.text):
            continue
        signals = unit_signals(unit, profile_terms)
        unclaimed.append(
            {
                "unit_id": unit.unit_id,
                "doc_id": unit.doc_id,
                "doc_role": unit.doc_role,
                "kind": unit.kind,
                "text": unit.text,
                "signals": signals,
                "suspected": bool(signals),
                "blocking": is_blocking(signals),
            }
        )
    unclaimed.sort(key=lambda item: (not item["blocking"], not item["suspected"]))
    return {
        "documents": documents,
        "unclaimed": unclaimed,
        "blocking_count": sum(1 for item in unclaimed if item["blocking"]),
    }
