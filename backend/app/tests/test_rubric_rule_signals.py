import pytest

from backend.app.services.rubric_import.classification.signals import is_noise
from backend.app.services.rubric_import.classification.signals import profile_signal_terms
from backend.app.services.rubric_import.classification.signals import rule_signals


@pytest.mark.parametrize(
    "text, expected",
    [
        ("全文错别字每处扣0.5分", {"score", "verb"}),
        ("查重率超过30%扣10分", {"score", "verb"}),
        ("正文不少于800字", {"normative"}),
        ("图表须有题注", {"normative"}),
        ("each missing figure loses 2 points", {"score"}),
        ("不得分", {"verb"}),
    ],
)
def test_generic_signals(text, expected):
    assert set(rule_signals(text)) == expected


@pytest.mark.parametrize("text", ["本章约3000字", "说明研究背景、研究意义和论文结构。", "第三章 研究方法", ""])
def test_plain_content_has_no_signal(text):
    assert rule_signals(text) == ()


def test_profile_terms_add_profile_signal():
    assert rule_signals("参考文献著录格式", profile_terms=("参考文献",)) == ("profile",)
    assert "参考文献" in profile_signal_terms("thesis")
    assert profile_signal_terms("unknown-profile") == ()


@pytest.mark.parametrize("text", ["", "  ", "—", "12", "（3）", "第 2 页", "-1-"])
def test_noise(text):
    assert is_noise(text) is True


@pytest.mark.parametrize("text", ["C01", "选题", "扣2分"])
def test_not_noise(text):
    assert is_noise(text) is False
