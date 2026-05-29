from types import SimpleNamespace

from backend.app.services.checkers import run_deterministic_checker


def _criterion(name, max_score=10.0):
    return SimpleNamespace(id="c-%s" % name, name=name, max_score=max_score)


def test_structure_completeness_deducts_per_missing_section():
    parsed = {
        "structure_checks": [
            {"code": "HAS_ABSTRACT_CN", "name": "中文摘要", "passed": True, "message": "检测到中文摘要", "location": "摘要"},
            {"code": "HAS_CONCLUSION", "name": "结论", "passed": False, "message": "未检测到结论", "location": None},
        ],
        "full_text": "正文",
        "references": [],
    }
    out = run_deterministic_checker(_criterion("写作规范", 10), parsed)
    assert out["score"] == 5.0
    assert out["confidence"] == 1.0
    assert out["scoring_mode"] == "deterministic"
    assert out["deduction_items"][0]["rule_ref"] == "HAS_CONCLUSION"
    assert out["deduction_items"][0]["points"] == 5.0


def test_word_count_below_minimum_scales_score():
    parsed = {"structure_checks": [], "full_text": "字" * 1500, "references": []}
    out = run_deterministic_checker(_criterion("正文字数", 10), parsed)
    assert out["checker_kind"] == "word_count"
    assert out["score"] == 5.0  # 1500/3000 * 10


def test_citation_flags_missing_reference_entry():
    parsed = {
        "structure_checks": [],
        "full_text": "研究见[1]，又见[3]，参考文献如下。",
        "references": ["[1] 张三. 研究[J]. 2024."],
    }
    out = run_deterministic_checker(_criterion("参考文献", 10), parsed)
    assert out["checker_kind"] == "citation"
    assert any(item["rule_ref"] == "CITATION_MISSING" for item in out["deduction_items"])
    assert out["score"] < 10.0


def test_citation_author_year_defers_to_review():
    parsed = {
        "structure_checks": [],
        "full_text": "已有研究（张三, 2020）表明……（李四, 2019）进一步说明。",
        "references": ["张三. 研究. 2020."],
    }
    out = run_deterministic_checker(_criterion("参考文献规范", 10), parsed)
    assert out["checker_kind"] == "citation_author_year"
    assert out["need_manual_review"] is True


def test_figure_check_deducts_when_no_references():
    parsed = {"structure_checks": [], "full_text": "本文不含任何图示编号引用。", "references": []}
    out = run_deterministic_checker(_criterion("图表规范", 10), parsed)
    assert out["checker_kind"] == "figure"
    assert out["score"] == 5.0
    assert out["need_manual_review"] is True
