from backend.app.services.coherence.terminology import analyze_terminology


def _kinds(findings):
    return {f["kind"] for f in findings}


def test_abbr_conflict_when_one_abbr_two_fulls():
    text = "卷积神经网络（CNN）是基础。后文又写成 循环神经网络（CNN）造成混淆。"
    findings = analyze_terminology(text)
    conflict = next(f for f in findings if f["kind"] == "terminology_abbr_conflict")
    assert "CNN" in conflict["message"]
    assert conflict["severity"] == "warning"
    assert set(conflict["refs"]) == {"卷积神经网络", "循环神经网络"}


def test_full_conflict_when_one_full_two_abbrs():
    text = "支持向量机（SVM）很常用，但有的章节写支持向量机（SV）显得不统一。"
    findings = analyze_terminology(text)
    conflict = next(f for f in findings if f["kind"] == "terminology_full_conflict")
    assert "支持向量机" in conflict["message"]
    assert set(conflict["refs"]) == {"SVM", "SV"}


def test_variant_clustering_casing():
    text = "我们用 Transformer 建模。后面又写 transformer，最后再提 transformer 一次。"
    findings = analyze_terminology(text)
    variant = next(f for f in findings if f["kind"] == "terminology_variant")
    assert variant["severity"] == "info"
    assert "Transformer" in variant["message"]
    assert "transformer" in variant["message"]


def test_consistent_text_no_findings():
    text = "卷积神经网络（CNN）贯穿全文，后续均用 CNN 与 CNN 表述，写法统一。"
    findings = analyze_terminology(text)
    assert "terminology_abbr_conflict" not in _kinds(findings)
    assert "terminology_full_conflict" not in _kinds(findings)


def test_lowercase_only_paren_not_treated_as_abbr():
    # 括号内无大写字母不当作缩写定义，避免误报。
    text = "这是一个示例（demo）用于说明，再次提到示例（demo）也不应报缩写冲突。"
    findings = analyze_terminology(text)
    assert "terminology_abbr_conflict" not in _kinds(findings)
