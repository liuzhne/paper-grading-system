from backend.app.services.rubric_import.coverage import compute_coverage
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit


def _add(ledger, unit_id, text, *, doc_id="rules", role="rules", kind="cell"):
    ledger.register(SourceUnit(unit_id, doc_id, role, kind, text, {}))


def _doc(report, doc_id):
    return next(item for item in report["documents"] if item["doc_id"] == doc_id)


def test_rules_document_ratio_excludes_structural_and_ignored_and_does_not_count_context():
    ledger = SourceLedger()
    for index in range(6):
        _add(ledger, f"u{index}", f"内容{index}")
    ledger.claim("u0", "C01.name")
    ledger.claim("u1", "C01.max_score")
    ledger.mark("u2", "structural", reason="表头")
    ledger.mark("u3", "ignored_by_rule", reason="非首张规则表")
    ledger.mark("u4", "context")
    report = compute_coverage(ledger)
    doc = _doc(report, "rules")
    assert doc["denominator"] == 4
    assert doc["handled"] == 2
    assert doc["ratio"] == 0.5
    assert doc["counts"] == {"consumed": 2, "context": 1, "structural": 1, "ignored_by_rule": 1, "unclaimed": 1}


def test_template_document_only_counts_comments_and_signal_paragraphs():
    ledger = SourceLedger()
    _add(ledger, "p1", "说明研究背景。", doc_id="tpl", role="template", kind="paragraph")
    _add(ledger, "p2", "正文不少于800字，否则扣2分。", doc_id="tpl", role="template", kind="paragraph")
    _add(ledger, "c1", "这里改一下", doc_id="tpl", role="template", kind="comment")
    _add(ledger, "c2", "缺失扣3分", doc_id="tpl", role="template", kind="comment")
    ledger.mark("p1", "context")
    ledger.claim("c2", "C01.deduction_rules")
    doc = _doc(compute_coverage(ledger), "tpl")
    assert doc["denominator"] == 3  # p2 + c1 + c2
    assert doc["handled"] == 1
    assert round(doc["ratio"], 4) == round(1 / 3, 4)


def test_reference_document_has_no_ratio_but_lists_suspected_rules():
    ledger = SourceLedger()
    _add(ledger, "r1", "迟交一天扣5分", doc_id="ref", role="reference", kind="paragraph")
    _add(ledger, "r2", "学院简介", doc_id="ref", role="reference", kind="paragraph")
    report = compute_coverage(ledger)
    assert _doc(report, "ref")["ratio"] is None
    assert [item["unit_id"] for item in report["unclaimed"] if item["suspected"]] == ["r1"]


def test_unclaimed_list_puts_suspected_first_and_counts_blockers():
    ledger = SourceLedger()
    _add(ledger, "a", "XX大学评分表")
    _add(ledger, "b", "错别字每处扣0.5分")
    _add(ledger, "c", "批注", doc_id="tpl", role="template", kind="comment")
    report = compute_coverage(ledger)
    assert [item["unit_id"] for item in report["unclaimed"]] == ["b", "c", "a"]
    assert report["unclaimed"][0]["signals"] == ["score", "verb"]
    assert report["unclaimed"][1]["signals"] == ["comment"]
    assert report["blocking_count"] == 2


def test_noise_units_are_never_suspected_and_empty_document_ratio_is_one():
    ledger = SourceLedger()
    _add(ledger, "n", "12")
    ledger.mark("n", "structural", reason="序号")
    report = compute_coverage(ledger)
    assert _doc(report, "rules")["ratio"] == 1.0
    assert report["unclaimed"] == []
    assert compute_coverage(SourceLedger()) == {"documents": [], "unclaimed": [], "blocking_count": 0}


def test_profile_terms_mark_units_as_suspected_but_not_blocking():
    """领域词与规范用语只提示，不阻断：否则几乎每份论文模板正文都会阻断发布。"""

    ledger = SourceLedger()
    _add(ledger, "a", "参考文献著录格式")
    assert compute_coverage(ledger)["unclaimed"][0]["suspected"] is False
    report = compute_coverage(ledger, profile_terms=("参考文献",))
    assert report["unclaimed"][0]["suspected"] is True
    assert report["unclaimed"][0]["blocking"] is False
    assert report["blocking_count"] == 0


def test_normative_only_units_are_suspected_but_not_blocking():
    ledger = SourceLedger()
    _add(ledger, "a", "正文不少于800字")
    _add(ledger, "b", "缺摘要扣2分")
    report = compute_coverage(ledger)
    by_id = {item["unit_id"]: item for item in report["unclaimed"]}
    assert (by_id["a"]["suspected"], by_id["a"]["blocking"]) == (True, False)
    assert (by_id["b"]["suspected"], by_id["b"]["blocking"]) == (True, True)
    assert report["blocking_count"] == 1
