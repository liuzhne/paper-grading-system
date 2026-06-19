"""L1 章节数字摘要（纯函数，不影响评分）。"""

from backend.app.services.document_parser.summary import section_summaries


def test_section_summaries_counts_paragraphs_and_chars():
    parsed = {
        "sections": [
            {"title": "绪论", "paragraphs": [{"text": "abc"}, {"text": "de"}]},
            {"title": "结论", "paragraphs": [{"text": "xyz"}]},
        ]
    }
    assert section_summaries(parsed) == [
        {"title": "绪论", "paragraphs": 2, "chars": 5},
        {"title": "结论", "paragraphs": 1, "chars": 3},
    ]


def test_section_summaries_empty_and_missing_title():
    assert section_summaries({}) == []
    assert section_summaries({"sections": [{"paragraphs": []}]}) == [
        {"title": "（未命名章节）", "paragraphs": 0, "chars": 0}
    ]
