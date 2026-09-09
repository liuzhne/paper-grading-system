"""三文档同步规则本身要可机检（CLAUDE.md「三文档同步（MUST）」）。

写在文档里的规则会烂掉——这条测试盯住其中能自动判断的部分：三份文档都在、
各自回答的问题没被混淆、决策索引与详情不脱节、维护记录跟得上。

机器判断不了「这条决策记得对不对」，但能判断「记没记」。
"""

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCS = {name: ROOT / name for name in ("ARCHITECTURE.md", "DECISIONS.md", "RUNBOOK.md")}


def _read(name):
    return DOCS[name].read_text(encoding="utf-8")


def test_all_three_documents_exist():
    for name, path in DOCS.items():
        assert path.is_file(), name


def test_claude_md_states_the_rule_and_names_all_three():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "三文档同步" in claude
    for name in DOCS:
        assert name in claude, name


def test_each_document_declares_what_it_answers():
    """三份文档职责不同，混着写等于三份都查不到东西。"""
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "模块边界" in claude and "调用链" in claude      # ARCHITECTURE
    assert "放弃了什么" in claude                            # DECISIONS
    assert "排错" in claude and "发布" in claude             # RUNBOOK


def test_decision_index_and_detail_sections_agree():
    """索引里有的决策必须有详情，反之亦然。

    只加索引不写详情，等于只记了「改过」；只写详情不加索引，下次没人找得到。
    """
    decisions = _read("DECISIONS.md")
    indexed = set(re.findall(r"^\| (D-\d+) \|", decisions, re.M))
    detailed = set(re.findall(r"^### (D-\d+)[：:]", decisions, re.M))

    assert indexed, "决策索引为空"
    assert indexed == detailed, (
        "索引与详情不一致：只在索引 %s；只在详情 %s"
        % (sorted(indexed - detailed), sorted(detailed - indexed))
    )


def test_every_decision_records_what_was_given_up():
    """「放弃」是这份文档的核心。

    只写选了什么，读的人无法判断当初是没想到别的方案，还是想到了并且有理由不选。
    """
    decisions = _read("DECISIONS.md")
    blocks = re.split(r"\n(?=### D-\d+)", decisions)
    missing = []
    for block in blocks:
        header = re.match(r"### (D-\d+)", block)
        if not header:
            continue
        if "放弃" not in block:
            missing.append(header.group(1))

    assert not missing, "这些决策没写放弃了什么：%s" % ", ".join(missing)


def test_each_document_keeps_a_maintenance_log():
    for name in DOCS:
        assert "维护记录" in _read(name), name


def test_maintenance_logs_cover_the_latest_decision_date():
    """三份文档的维护记录要跟得上最新决策。

    只改代码不改文档时，这条会先红。
    """
    decisions = _read("DECISIONS.md")
    dates = re.findall(r"20\d\d-\d\d-\d\d", decisions)
    assert dates
    latest = max(dates)

    for name in DOCS:
        assert latest in _read(name), "%s 的维护记录停在 %s 之前" % (name, latest)
