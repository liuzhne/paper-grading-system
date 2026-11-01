"""Word 模板读全内容、识别章节、组条目（docs/Word评分规范抽取与对应方案.md 分期 A）。不调用模型。"""

import json
from collections import Counter
from io import BytesIO

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.oxml.ns import qn

from backend.app.core.config import settings
from backend.app.scripts import inspect_requirement_items
from backend.app.services.rubric_import import parse_state
from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.extraction.docx_extractor import mark_remaining_blocks
from backend.app.services.rubric_import.requirements.items import MAX_LIST_ENTRIES
from backend.app.services.rubric_import.requirements.items import build_items
from backend.app.services.rubric_import.requirements.items import render_item
from backend.app.services.rubric_import.requirements.outline import build_outline
from backend.app.services.rubric_import.requirements.outline import section_path
from backend.app.services.rubric_import.requirements.state import assemble_requirements
from backend.app.services.rubric_import.requirements.state import template_read_warnings
from backend.app.services.rubric_import.sources.docx_adapter import load_docx
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit
from backend.app.tests import requirement_doc_fixtures as docs
from backend.app.tests import rubric_parse_fixtures as fx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _read(data, deep=True):
    ledger = SourceLedger()
    view = load_docx(data, ledger, doc_id="word", doc_role="template", deep=deep)
    return view, ledger


def _build(data):
    view, ledger = _read(data)
    outline = build_outline(view.structure)
    return view, ledger, outline, build_items(view.structure, outline, view.comments)


def _texts(ledger):
    return {unit.unit_id: unit.text for unit in ledger.units()}


def _document(*paragraphs):
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    return document


def _nodes(*paragraphs):
    """段落序列的章节识别结果：标题文字 → (层级, 依据)。"""

    view, _, outline, _ = _build(docs.save(_document(*paragraphs)))
    return {node["title"]: (node["level"], node["source"]) for node in outline["nodes"]}


# ---- 第 1 层：读全内容 ----


def test_deep_reads_every_structure_once():
    view, ledger = _read(docs.everything_docx())
    texts = _texts(ledger)
    assert texts["docx:p[16]"] == "密钥须定期轮换，周期不超过 90 天"  # 修订插入进来，删除和移走的不进
    assert texts["docx:p[17]"] == "整段都是修订插入：须保留审计日志"
    assert "已删除的文字" not in "".join(texts.values())
    assert texts["docx:p[18]"] == "正文和文本框同段"
    assert texts["docx:p[18]/txbx[0]"] == "文本框要求：必须写明并发量"
    assert list(texts.values()).count("文本框要求：必须写明并发量") == 1  # Fallback 不重复
    assert texts["docx:sdt[1]/p[0]"] == "内容控件要求：须提供部署拓扑"
    assert texts["docx:tbl[1]/r1/c1/tbl[0]/r0/c0"] == "嵌套表格要求：须统一编码"
    assert ledger.unit("docx:tbl[1]/r1/c1/tbl[0]/r0/c0").kind == "table_cell"
    assert "一、需求分析要求 扣分说明..........1" not in texts.values()  # 目录控件不读
    assert texts["docx:p[20]/img[0]"] == "[图片]"
    assert ledger.unit("docx:p[20]/img[0]").kind == "image"
    assert ledger.unit("docx:tbl[1]/r2/c1/p[0]/img[0]").kind == "image"
    image = ledger.unit("docx:p[20]/img[0]").context["image"]
    assert image["content_type"] == "image/png"
    assert image["size_bytes"] == len(docs.PNG)
    assert len(image["sha256"]) == 64
    assert view.tables and len(view.tables) == 2  # 嵌套表格和控件里的表格不进 view.tables


def test_inline_control_and_hyperlink_text_stay_in_the_paragraph():
    document = Document()
    paragraph = document.add_paragraph("前文")
    paragraph._p.append(parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtContent><w:r><w:t>行内控件</w:t></w:r></w:sdtContent></w:sdt>'))
    paragraph._p.append(parse_xml(f'<w:hyperlink {nsdecls("w")}><w:r><w:t>超链接</w:t></w:r></w:hyperlink>'))
    view, ledger = _read(docs.save(document))
    assert ledger.unit("docx:p[0]").text == "前文行内控件超链接"
    assert view.read_report["missing_chars"] == 0


def test_tables_inside_controls_and_controls_inside_cells():
    document = Document()
    control = docs.add_sdt(document, "控件第一段", "控件第二段")
    content = control.find(qn("w:sdtContent"))
    table = document.add_table(rows=2, cols=2)
    for row, values in enumerate((("项目", "内容"), ("编码", "须统一"))):
        for col, value in enumerate(values):
            table.cell(row, col).text = value
    content.append(table._tbl)  # 移进内容控件
    content.append(parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtContent><w:p><w:r><w:t>嵌套控件</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
    outer = document.add_table(rows=1, cols=1)
    outer.cell(0, 0)._tc.append(
        parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtContent><w:p><w:r><w:t>单元格里的控件</w:t></w:r></w:p></w:sdtContent></w:sdt>')
    )
    view, ledger, outline, items = _build(docs.save(document))
    texts = _texts(ledger)
    assert texts["docx:sdt[0]/p[1]"] == "控件第二段"
    assert texts["docx:sdt[0]/tbl[0]/r1/c1"] == "须统一"
    assert texts["docx:sdt[0]/sdt[0]/p[0]"] == "嵌套控件"
    assert texts["docx:tbl[0]/r0/c0"] == "单元格里的控件"
    assert view.read_report["missing_chars"] == 0
    assert len(view.tables) == 1  # 控件里的表格不进 view.tables，表格编号不变
    assert "项目：编码；内容：须统一" in [render_item(item, ledger) for item in items["items"]]
    _assert_invariants(ledger, outline, items)


def test_text_box_and_image_inside_a_table_cell():
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "内容"
    table.cell(1, 0).text = "架构"
    cell = table.cell(1, 1)
    cell.paragraphs[0].add_run().add_picture(BytesIO(docs.PNG))
    docs.add_text_box(cell.add_paragraph(""), "格内文本框要求")
    view, ledger, outline, items = _build(docs.save(document))
    row = items["items"][0]
    assert row["unit_ids"] == ["docx:tbl[0]/r1/c0", "docx:tbl[0]/r1/c1/p[1]/txbx[0]"]
    assert row["image_unit_ids"] == ["docx:tbl[0]/r1/c1/p[0]/img[0]"]
    assert render_item(row, ledger) == "项目：架构；格内文本框要求\n[图片]"
    _assert_invariants(ledger, outline, items)


def test_merged_empty_cell_with_image_is_read_once():
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "内容"
    table.cell(1, 0).merge(table.cell(1, 1)).paragraphs[0].add_run().add_picture(BytesIO(docs.PNG))
    view, ledger, outline, items = _build(docs.save(document))
    assert [unit.unit_id for unit in ledger.units() if unit.kind == "image"] == ["docx:tbl[0]/r1/c0/p[0]/img[0]"]
    _assert_invariants(ledger, outline, items)


def test_image_alt_text_goes_into_label_and_fallback_image_is_not_duplicated():
    document = Document()
    document.add_picture(BytesIO(docs.PNG))
    picture = document.paragraphs[-1]
    properties = picture._p.xpath(".//wp:docPr")[0]
    properties.set("descr", "系统架构图")
    blip = picture._p.xpath(".//a:blip")[0]
    relation = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
    picture._p.append(
        parse_xml(
            f'<w:r {nsdecls("w")} xmlns:mc="{docs.MC}" xmlns:v="{docs.V}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><mc:AlternateContent>'
            f'<mc:Choice Requires="wps"><w:t/></mc:Choice><mc:Fallback><w:pict><v:shape><v:imagedata r:id="{relation}"/>'
            "</v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
        )
    )
    _, ledger = _read(docs.save(document))
    images = [unit for unit in ledger.units() if unit.kind == "image"]
    assert [unit.text for unit in images] == ["[图片：系统架构图]"]


def test_vml_image_without_alternate_content_is_read():
    document = Document()
    document.add_picture(BytesIO(docs.PNG))
    picture = document.paragraphs[-1]
    relation = picture._p.xpath(".//a:blip")[0].get(
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
    )
    legacy = document.add_paragraph()
    legacy._p.append(
        parse_xml(
            f'<w:r {nsdecls("w")} xmlns:v="{docs.V}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:pict>'
            f'<v:shape alt="旧版图片"><v:imagedata r:id="{relation}"/></v:shape></w:pict></w:r>'
        )
    )
    _, ledger = _read(docs.save(document))
    assert ledger.unit("docx:p[1]/img[0]").text == "[图片：旧版图片]"


@pytest.mark.parametrize(
    "factory",
    [
        fx.template_docx_with_comments,
        fx.template_docx_with_rule_table,
        fx.rules_docx,
        fx.rules_docx_paragraphs,
        *(lambda seed=seed: docs.random_docx(seed, revisions=False) for seed in range(30)),
    ],
)
def test_deep_keeps_existing_units_when_there_are_no_revisions(factory):
    data = factory()
    _, shallow = _read(data, deep=False)
    _, deep = _read(data)
    for unit in shallow.units():
        same = deep.unit(unit.unit_id)
        assert (same.kind, same.text) == (unit.kind, unit.text)
        assert same.context.get("heading_path") == unit.context.get("heading_path")


def test_revisions_only_add_units_and_text():
    data = docs.everything_docx()
    _, shallow = _read(data, deep=False)
    _, deep = _read(data)
    for unit in shallow.units():
        assert unit.text in deep.unit(unit.unit_id).text
    assert deep.unit("docx:p[16]").text != shallow.unit("docx:p[16]").text


def test_plain_text_matches_python_docx_rules():
    document = Document()
    paragraph = document.add_paragraph("第一行")
    run = paragraph.add_run("第二行")
    run.add_break()
    run.add_text("第三行")
    run.add_tab()
    run.add_text("第四行")
    paragraph._p.get_or_add_pPr().append(
        parse_xml(f'<w:tabs {nsdecls("w")}><w:tab w:val="left" w:pos="720"/></w:tabs>')
    )
    _, deep = _read(docs.save(document))
    _, shallow = _read(docs.save(document), deep=False)
    assert deep.unit("docx:p[0]").text == shallow.unit("docx:p[0]").text


def test_shallow_mode_reads_nothing_new():
    view, ledger = _read(docs.everything_docx(), deep=False)
    assert view.structure == [] and view.read_report is None and view.doc_id is None
    assert not [unit for unit in ledger.units() if unit.kind == "image"]
    assert not [unit for unit in ledger.units() if "txbx" in unit.unit_id or "sdt" in unit.unit_id]
    assert not ledger.has("docx:p[17]")  # 整段修订插入：和现在一样读不到


@pytest.mark.parametrize("seed", range(40))
def test_read_report_has_nothing_missing_for_supported_structures(seed):
    view, _ = _read(docs.random_docx(seed))
    assert view.read_report["missing_chars"] == 0, view.read_report


def test_read_report_finds_structures_we_cannot_read():
    document = _document("正文")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "普通行"
    table._tbl.append(
        parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtContent><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>藏在控件里的行</w:t></w:r></w:p>'
            "</w:tc></w:tr></w:sdtContent></w:sdt>"
        )
    )
    document.sections[0].header.paragraphs[0].text = "页眉说明"
    document.sections[0].footer.paragraphs[0].text = "页脚"
    view, _ = _read(docs.save(document))
    report = view.read_report
    assert report["missing_chars"] == len("藏在控件里的行")
    assert set(report["missing_sample"]) == set("藏在控件里的行")
    assert report["unread_parts"] == {"页眉": 4, "页脚": 2}
    warnings = template_read_warnings(report)
    assert [item["code"] for item in warnings] == ["TEMPLATE_READ_INCOMPLETE"]
    assert "7 个字符" in warnings[0]["message"] and "页眉 4 字" in warnings[0]["message"]


def test_no_warning_when_everything_was_read():
    view, _ = _read(docs.save(_document("正文")))
    assert template_read_warnings(view.read_report) == []
    assert template_read_warnings(None) == []


def test_charts_and_embedded_objects_are_counted():
    document = _document("正文")
    document.paragraphs[0]._p.append(
        parse_xml(f'<w:r {nsdecls("w")}><w:object/></w:r>')
    )
    view, _ = _read(docs.save(document))
    assert view.read_report["unread_objects"] == 1
    assert "1 个图表或嵌入对象" in template_read_warnings(view.read_report)[0]["message"]


def test_toc_style_and_leader_paragraphs_are_flagged():
    document = Document()
    document.styles.add_style("TOC 1", 1)
    document.add_paragraph("3.1 扣分规则\t12", style="TOC 1")
    document.add_paragraph("一、格式要求（10分）……3")
    document.add_paragraph("正文")
    view, ledger = _read(docs.save(document))
    assert ledger.unit("docx:p[0]").context["toc"] is True
    assert ledger.unit("docx:p[1]").context["toc"] is True
    assert "toc" not in ledger.unit("docx:p[2]").context
    assert [block["unit_id"] for block in view.blocks] == ["docx:p[2]"]  # 目录行不当评分项候选


# ---- 第 2 层：识别章节 ----


def test_heading_priority_and_levels():
    document = Document()
    document.add_paragraph("技术方案模板", style="Title")
    document.add_heading("样式标题", level=2)
    docs.add_outline_paragraph(document, "大纲标题", level=2)
    document.add_paragraph("一、中文一级")
    document.add_paragraph("（一）中文二级")
    document.add_paragraph("1.1 小数二级")
    document.add_paragraph("1.1.1 小数三级")
    document.add_paragraph("第二章 总体设计")
    document.add_paragraph("第三节 接口")
    document.add_paragraph("参考文献")
    view, _, outline, _ = _build(docs.save(document))
    nodes = {node["title"]: (node["level"], node["source"]) for node in outline["nodes"]}
    assert nodes == {
        "技术方案模板": (0, "style"),
        "样式标题": (2, "style"),
        "大纲标题": (3, "outline"),
        "一、中文一级": (1, "text_number"),
        "（一）中文二级": (2, "text_number"),
        "1.1 小数二级": (2, "text_number"),
        "1.1.1 小数三级": (3, "text_number"),
        "第二章 总体设计": (1, "text_number"),
        "第三节 接口": (2, "text_number"),
        "参考文献": (1, "keyword"),
    }


def test_outline_parent_chain_and_section_path():
    document = Document()
    document.add_heading("一级", level=1)
    document.add_heading("二级", level=2)
    document.add_heading("三级", level=3)
    document.add_heading("另一个二级", level=2)
    _, _, outline, _ = _build(docs.save(document))
    nodes = outline["nodes"]
    assert [node["section_id"] for node in nodes] == ["S1", "S2", "S3", "S4"]
    assert [node["parent_id"] for node in nodes] == [None, "S1", "S2", "S1"]
    assert section_path(outline, "S3") == ["一级", "二级", "三级"]
    assert section_path(outline, "S4") == ["一级", "另一个二级"]
    assert section_path(outline, None) == []


@pytest.mark.parametrize(
    "text",
    [
        "三、格式错误扣 5 分",  # 带分值的不算标题，门禁才看得到
        "一、需求分析（10分）",
        "二、总体要求：",  # 句末标点
        "一、接口文档须给出错误码说明。",
        "一、" + "很长" * 21,  # 超过 40 字
    ],
)
def test_text_numbers_that_are_not_headings(text):
    assert _nodes(text, "下一段") == {}


def test_verb_heading_is_a_heading_with_an_item():
    view, ledger, outline, items = _build(docs.save(_document("三、扣分规则", "正文段落")))
    assert [node["title"] for node in outline["nodes"]] == ["三、扣分规则"]
    assert [(item["kind"], item["unit_ids"]) for item in items["items"]][0] == ("heading", ["docx:p[0]"])


def test_heading_with_any_rule_signal_also_gets_an_item():
    """标题也可能就是一条规范，不能因为被当成标题就从条目里消失。"""

    _, _, outline, items = _build(docs.save(_document("3.2 接口须统一编码", "下一段", "一、需求分析", "另一段")))
    assert [node["title"] for node in outline["nodes"]] == ["3.2 接口须统一编码", "一、需求分析"]
    kinds = {item["unit_ids"][0]: item["kind"] for item in items["items"]}
    assert kinds["docx:p[0]"] == "heading"
    assert "docx:p[2]" not in kinds  # “需求”里的“需”不算规范用语


def test_number_followed_by_a_measure_word_is_not_a_heading():
    assert _nodes("5 个工作日内回复", "正文说明段落") == {}
    assert _nodes("1 概述", "正文说明段落") == {"1 概述": (1, "text_number")}


def test_auto_numbered_paragraph_is_never_a_text_heading():
    document = Document()
    document.add_paragraph("一、自动编号的列表项", style="List Number")
    document.add_paragraph("下一段")
    _, _, outline, _ = _build(docs.save(document))
    assert outline["nodes"] == []


def test_decimal_marker_is_heading_only_when_isolated():
    assert _nodes("1、概述", "正文说明段落") == {"1、概述": (3, "text_number")}
    # 连续序号是列表，最后一项也不能被当成标题
    assert _nodes("1、分层设计", "2、部署拓扑", "正文说明段落") == {}
    # 引导句后面的是列表
    assert _nodes("总体架构须包括：", "1、分层设计", "正文说明段落") == {}
    # 文档最后一段不算标题
    assert _nodes("正文说明段落", "1、结束") == {}


def test_decimal_marker_before_table_is_a_heading():
    document = _document("1、错误码规范")
    docs.fill_table(document, [("检查项", "要求"), ("错误码", "须统一编码")])
    _, _, outline, _ = _build(docs.save(document))
    assert [node["title"] for node in outline["nodes"]] == ["1、错误码规范"]


def test_chinese_second_level_after_lead_is_a_list_entry():
    assert _nodes("主要包括以下内容：", "（一）分层设计", "（二）部署拓扑") == {}


def test_bold_heading_rules():
    document = Document()
    document.add_paragraph().add_run("交付物清单").bold = True
    document.add_paragraph("需提交源码和文档。")
    document.add_paragraph().add_run("连续加粗一").bold = True
    document.add_paragraph().add_run("连续加粗二").bold = True
    document.add_paragraph("正文说明段落。")
    mixed = document.add_paragraph()
    mixed.add_run("部分加粗").bold = True
    mixed.add_run("部分不加粗")
    document.add_paragraph().add_run("最后一段加粗").bold = True
    _, _, outline, _ = _build(docs.save(document))
    assert [(node["title"], node["level"], node["source"]) for node in outline["nodes"]] == [
        ("交付物清单", 4, "bold"),
        ("连续加粗二", 4, "bold"),
    ]


def test_bold_inherited_from_style_counts():
    document = Document()
    style = document.styles.add_style("要点", 1)
    style.font.bold = True
    document.add_paragraph("样式加粗的小标题", style="要点")
    document.add_paragraph("正文说明段落。")
    _, _, outline, _ = _build(docs.save(document))
    assert [node["source"] for node in outline["nodes"]] == ["bold"]


def test_text_box_is_never_a_heading():
    document = Document()
    docs.add_text_box(document.add_paragraph(""), "一、文本框里的字")
    document.add_paragraph("正文说明段落")
    _, _, outline, _ = _build(docs.save(document))
    assert outline["nodes"] == []


# ---- 第 3 层：组条目 ----


def _rendered(data):
    _, ledger, _, items = _build(data)
    return [render_item(item, ledger) for item in items["items"]]


def test_list_merges_into_lead():
    _, ledger, _, items = _build(docs.save(_document("总体架构须包括以下内容：", "1、分层设计", "2、部署拓扑", "另一段正文。")))
    assert [item["kind"] for item in items["items"]] == ["list", "paragraph"]
    assert items["items"][0]["unit_ids"] == ["docx:p[0]", "docx:p[1]", "docx:p[2]"]
    assert render_item(items["items"][0], ledger) == "总体架构须包括以下内容：\n1、分层设计\n2、部署拓扑"


def test_auto_numbered_list_merges_by_num_id():
    document = _document("部署方案须包括：")
    document.add_paragraph("主备切换", style="List Number")
    document.add_paragraph("回滚步骤", style="List Number")
    document.add_paragraph("项目符号项", style="List Bullet")
    _, _, _, items = _build(docs.save(document))
    assert [(item["kind"], item["unit_ids"]) for item in items["items"]] == [
        ("list", ["docx:p[0]", "docx:p[1]", "docx:p[2]"]),
        ("paragraph", ["docx:p[3]"]),
    ]


def test_list_without_lead_stays_separate():
    _, _, _, items = _build(docs.save(_document("1、接口文档应给出错误码说明。", "2、鉴权不得明文传输。")))
    assert [item["kind"] for item in items["items"]] == ["paragraph", "paragraph"]


def test_long_list_keeps_entries_separate_with_lead_as_context():
    entries = [f"{index}、第{index}项要求" for index in range(1, MAX_LIST_ENTRIES + 2)]
    _, ledger, _, items = _build(docs.save(_document("须包括以下内容：", *entries)))
    assert [item["kind"] for item in items["items"]] == ["paragraph"] * (MAX_LIST_ENTRIES + 2)
    assert items["items"][1]["context_unit_ids"] == ["docx:p[0]"]
    assert render_item(items["items"][1], ledger) == "（上文：须包括以下内容：）\n1、第1项要求"


def test_list_at_the_limit_still_merges():
    entries = [f"{index}、第{index}项要求" for index in range(1, MAX_LIST_ENTRIES + 1)]
    _, _, _, items = _build(docs.save(_document("须包括以下内容：", *entries)))
    assert [item["kind"] for item in items["items"]] == ["list"]


def test_table_rows_with_header_and_vertical_merge():
    document = Document()
    table = docs.fill_table(
        document, [("检查项", "要求"), ("错误码", "须统一编码"), ("", "须有中文说明"), ("鉴权", "不得明文传输密钥")]
    )
    table.cell(1, 0).merge(table.cell(2, 0))
    table.cell(1, 0).text = "错误码"
    _, ledger, _, items = _build(docs.save(document))
    rows = items["items"]
    assert items["table_headers"] == ["docx:tbl[0]/r0/c0", "docx:tbl[0]/r0/c1"]
    assert rows[1]["unit_ids"] == ["docx:tbl[0]/r2/c1"]
    assert "docx:tbl[0]/r1/c0" in rows[1]["context_unit_ids"]
    assert [render_item(row, ledger) for row in rows] == [
        "检查项：错误码；要求：须统一编码",
        "检查项：错误码；要求：须有中文说明",
        "检查项：鉴权；要求：不得明文传输密钥",
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [("需求分析", "10分"), ("总体架构", "须分层")],  # 第一行有分值，其实是数据
        [("只有一列"), ("第二行")],
        [("只有一行", "两列")],
    ],
)
def test_tables_without_header(rows):
    document = Document()
    table = document.add_table(rows=len(rows), cols=1 if isinstance(rows[0], str) else len(rows[0]))
    for row_index, values in enumerate(rows):
        for col_index, value in enumerate([values] if isinstance(values, str) else values):
            table.cell(row_index, col_index).text = value
    _, _, _, items = _build(docs.save(document))
    assert items["table_headers"] == []
    assert len(items["items"]) == len(rows)


def test_merged_title_row_is_not_a_header():
    document = Document()
    table = docs.fill_table(document, [("技术评分表", ""), ("错误码", "须统一编码")])
    table.cell(0, 0).merge(table.cell(0, 1))
    _, _, _, items = _build(docs.save(document))
    assert items["table_headers"] == []


def test_noise_only_rows_are_excluded():
    document = Document()
    docs.fill_table(document, [("检查项", "要求"), ("—", "1"), ("错误码", "须统一")])
    _, _, _, items = _build(docs.save(document))
    assert [item["unit_ids"] for item in items["items"]] == [["docx:tbl[0]/r2/c0", "docx:tbl[0]/r2/c1"]]
    assert {entry["reason"] for entry in items["excluded"]} == {"噪声"}


def test_nested_table_rows_follow_the_row_that_holds_them():
    document = Document()
    outer = docs.fill_table(document, [("项目", "内容"), ("第一", "甲"), ("第二", "乙")])
    outer.cell(1, 1).add_table(rows=1, cols=1).cell(0, 0).text = "嵌套"
    _, _, _, items = _build(docs.save(document))
    assert [item["unit_ids"][0] for item in items["items"]] == [
        "docx:tbl[0]/r1/c0",
        "docx:tbl[0]/r1/c1/tbl[0]/r0/c0",
        "docx:tbl[0]/r2/c0",
    ]


def test_comment_merges_into_the_item_it_marks():
    document = Document()
    document.add_paragraph("性能指标须写明：")
    entry = document.add_paragraph("1、并发量")
    document.add_comment(entry.runs, text="写明峰值", author="r")
    table = docs.fill_table(document, [("检查项", "要求"), ("错误码", "须统一编码")])
    document.add_comment(table.cell(1, 1).paragraphs[0].runs, text="按模块分段", author="r")
    _, ledger, _, items = _build(docs.save(document))
    assert [item["kind"] for item in items["items"]] == ["list", "table_row"]
    assert items["items"][0]["comment_unit_ids"] == ["docx:comment[0]"]
    assert items["items"][1]["comment_unit_ids"] == ["docx:comment[1]"]
    assert render_item(items["items"][1], ledger) == "检查项：错误码；要求：须统一编码\n（批注：按模块分段）"


def test_comment_on_heading_makes_a_heading_item():
    document = Document()
    heading = document.add_heading("二、总体架构", level=1)
    document.add_comment(heading.runs, text="本章必须有架构图", author="r")
    _, ledger, _, items = _build(docs.save(document))
    assert [(item["kind"], item["section_id"]) for item in items["items"]] == [("heading", "S1")]
    assert "（批注：本章必须有架构图）" in render_item(items["items"][0], ledger)


def test_comment_on_table_header_stands_alone():
    document = Document()
    table = docs.fill_table(document, [("检查项", "要求"), ("错误码", "须统一编码")])
    document.add_comment(table.cell(0, 0).paragraphs[0].runs, text="表头批注", author="r")
    _, ledger, _, items = _build(docs.save(document))
    assert [item["kind"] for item in items["items"]] == ["comment", "table_row"]
    assert render_item(items["items"][0], ledger).startswith("（批注：表头批注")


def test_comment_without_a_location_stands_alone_with_its_anchor():
    document = Document()
    document.add_paragraph("正文")
    empty = document.add_paragraph("")
    run = empty.add_run("临时")
    document.add_comment(run, text="孤立批注", author="r")
    run.text = ""  # 批注所在段落没有文字，定位不到单元
    _, ledger, _, items = _build(docs.save(document))
    assert [item["kind"] for item in items["items"]] == ["paragraph", "comment"]
    assert items["items"][1]["unit_ids"] == []
    assert items["items"][1]["section_id"] is None


def test_image_joins_caption_or_lead():
    def image_doc(*parts):
        document = Document()
        for part in parts:
            if part == "IMG":
                document.add_picture(BytesIO(docs.PNG))
            else:
                document.add_paragraph(part)
        return docs.save(document)

    def shape(*parts):
        _, _, _, items = _build(image_doc(*parts))
        return [(item["kind"], len(item["unit_ids"]), len(item["image_unit_ids"])) for item in items["items"]]

    assert shape("IMG", "图 1 架构示意") == [("figure", 1, 1)]
    assert shape("图 1 架构示意", "IMG") == [("figure", 1, 1)]
    assert shape("架构图示例：", "IMG") == [("figure", 1, 1)]
    assert shape("架构图示例：", "IMG", "图 1 架构示意") == [("figure", 2, 1)]
    assert shape("架构图如下：", "IMG", "IMG") == [("figure", 1, 2)]
    assert shape("普通段落。", "IMG") == [("paragraph", 1, 0), ("image", 0, 1)]
    assert shape("IMG", "普通段落。") == [("image", 0, 1), ("paragraph", 1, 0)]
    assert shape("IMG", "图 1 甲", "IMG", "图 2 乙") == [("figure", 1, 1), ("figure", 1, 1)]
    assert shape("图 1 甲", "IMG", "图 2 乙", "IMG") == [("figure", 1, 1), ("figure", 1, 1)]


def test_inline_image_stays_on_its_paragraph():
    document = Document()
    document.add_paragraph("架构须分层").add_run().add_picture(BytesIO(docs.PNG))
    _, ledger, _, items = _build(docs.save(document))
    assert [item["kind"] for item in items["items"]] == ["paragraph"]
    assert render_item(items["items"][0], ledger) == "架构须分层\n[图片]"


def test_noise_and_total_statements_are_excluded_with_a_reason():
    _, _, _, items = _build(docs.save(_document("总分 100 分", "12", "第 3 页", "正文")))
    assert {entry["unit_id"]: entry["reason"] for entry in items["excluded"]} == {
        "docx:p[0]": "总分说明",
        "docx:p[1]": "噪声",
        "docx:p[2]": "噪声",
    }
    assert [item["unit_ids"] for item in items["items"]] == [["docx:p[3]"]]


def test_item_ids_are_stable_across_parses():
    first = _build(docs.everything_docx())[3]
    second = _build(docs.everything_docx())[3]
    assert [item["item_id"] for item in first["items"]] == [item["item_id"] for item in second["items"]]
    assert all(item["item_id"].startswith("R-") and len(item["item_id"]) == 12 for item in first["items"])


def _assert_invariants(ledger, outline, items):
    """第 6 节：每个 Word 单元只能属于一处；条目顺序和文档顺序一致。"""

    item_units = [unit for item in items["items"] for unit in item["unit_ids"]]
    owned = Counter(item_units)
    owned.update(items["table_headers"])
    owned.update(entry["unit_id"] for entry in items["excluded"])
    owned.update(node["unit_id"] for node in outline["nodes"] if node["unit_id"] not in item_units)
    comments = Counter(unit for item in items["items"] for unit in item["comment_unit_ids"])
    images = Counter(unit for item in items["items"] for unit in item["image_unit_ids"])
    expected = Counter()
    for unit in ledger.units():
        if unit.kind == "comment":
            assert comments[unit.unit_id] == 1, unit.unit_id
        elif unit.kind == "image":
            assert images[unit.unit_id] == 1, unit.unit_id
        else:
            expected[unit.unit_id] = 1
    assert owned == expected
    assert len(comments) == sum(comments.values())
    assert len(images) == sum(images.values())

    order = {unit.unit_id: index for index, unit in enumerate(ledger.units())}
    keys = [
        min(order[unit] for unit in (*item["unit_ids"], *item["image_unit_ids"]))
        for item in items["items"]
        if item["unit_ids"] or item["image_unit_ids"]
    ]
    assert keys == sorted(keys)
    ids = [item["item_id"] for item in items["items"]]
    assert len(ids) == len(set(ids))
    for item in items["items"]:
        assert item["unit_ids"] or item["comment_unit_ids"] or item["image_unit_ids"]
        assert all(ledger.has(unit) for unit in item["context_unit_ids"])
        assert render_item(item, ledger)


def test_invariants_on_the_sample_document():
    _, ledger, outline, items = _build(docs.everything_docx())
    _assert_invariants(ledger, outline, items)


@pytest.mark.parametrize("seed", range(150))
def test_invariants_on_random_documents(seed):
    _, ledger, outline, items = _build(docs.random_docx(seed))
    _assert_invariants(ledger, outline, items)


# ---- 导入流程 ----


def _command(profile_key="general_document"):
    return {
        "schema_version": pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "file_import",
        "rubric": {"name": "规范", "version": "v1", "business_profile_key": profile_key},
        "files": {"rules_file_name": "rules.xlsx", "template_file_name": "t.docx"},
        "compiler": {"parser_version": "p", "compiler_version": "c", "prompt_version": "none"},
        "version": {"hash_scheme": "rubric-content-v2"},
    }


def _prepare(template, rules=None):
    return pipeline.prepare_file_import(
        command=_command(), rules_bytes=rules or fx.simple_rules_xlsx(), template_bytes=template
    ).to_mapping()


def _ledger_of(graph):
    return SourceLedger.from_mapping(graph["compilation"]["raw_parse_output"]["source_ledger"])


@pytest.fixture
def requirement_items_on(monkeypatch):
    monkeypatch.setattr(settings, "REQUIREMENT_ITEMS_ENABLED", True)


def test_switch_defaults_off_and_import_is_unchanged():
    assert settings.REQUIREMENT_ITEMS_ENABLED is False
    graph = _prepare(docs.everything_docx())
    raw = graph["compilation"]["raw_parse_output"]
    assert "outline" not in raw and "requirement_items" not in raw
    units = _ledger_of(graph).units()
    assert not [unit for unit in units if unit.kind == "image" or "txbx" in unit.unit_id or "sdt" in unit.unit_id]
    assert not [item for item in graph["compilation"]["warnings"] if item.get("code") == "TEMPLATE_READ_INCOMPLETE"]


def test_word_only_import_never_goes_deep(requirement_items_on):
    graph = pipeline.prepare_file_import(command=_command(), rules_bytes=None, template_bytes=fx.rules_docx()).to_mapping()
    assert "requirement_items" not in graph["compilation"]["raw_parse_output"]


def test_switch_on_stores_outline_and_items(requirement_items_on):
    graph = _prepare(docs.everything_docx())
    raw = graph["compilation"]["raw_parse_output"]
    assert raw["outline"]["version"] == 1 and raw["requirement_items"]["version"] == 1
    assert raw["requirement_items"]["read_report"]["missing_chars"] == 0
    json.dumps(raw, ensure_ascii=False)  # 能落进 JSON 列
    _assert_invariants(
        SourceLedger.from_mapping(
            {"units": [u for u in raw["source_ledger"]["units"] if u["doc_id"] == "word"]}
        ),
        raw["outline"],
        raw["requirement_items"],
    )


def test_gate_changes_are_the_ones_the_plan_lists(requirement_items_on):
    document = Document()
    document.add_paragraph("3.1 扣分规则......12")  # 目录：不再阻断
    document.add_paragraph("四、必须遵守的规范")  # 标题，没有阻断信号：标 context
    document.add_paragraph("三、扣分规则")  # 标题，只有动词：仍然阻断，但有条目可处理
    docs.fill_table(document, [("检查项", "扣分标准"), ("错误码", "须统一")])  # 表头的“扣分”不是规则
    docs.add_text_box(document.add_paragraph(""), "错误码缺失扣 2 分")  # 以前读不到，现在阻断
    commented = document.add_paragraph("性能指标")
    document.add_comment(commented.runs, text="缺失扣 3 分", author="r")
    graph = _prepare(docs.save(document))
    ledger = _ledger_of(graph)
    raw = graph["compilation"]["raw_parse_output"]
    assert ledger.status("docx:p[0]").status == "structural"
    assert ledger.status("docx:p[0]").reason == "目录"
    assert ledger.status("docx:p[1]").status == "context"
    assert ledger.status("docx:p[2]").status == "unclaimed"
    assert ledger.status("docx:tbl[0]/r0/c1").status == "context"
    assert ledger.status("docx:p[3]/txbx[0]").status == "unclaimed"
    # 批注状态不变，否则每条批注都是新的阻断
    assert ledger.status("docx:comment[0]").status == "consumed"
    assert ledger.status("docx:comment[0]").claimed_by == ("template_items.WORD-COMMENT-0",)
    blocking = {item["unit_id"] for item in raw["coverage"]["unclaimed"] if item["blocking"] and item["doc_id"] == "word"}
    assert blocking == {"docx:p[2]", "docx:p[3]/txbx[0]"}
    heading = next(item for item in raw["requirement_items"]["items"] if item["unit_ids"] == ["docx:p[2]"])
    assert heading["kind"] == "heading"


def test_image_alt_text_does_not_block(requirement_items_on):
    document = _document("正文")
    document.add_picture(BytesIO(docs.PNG))
    document.paragraphs[-1]._p.xpath(".//wp:docPr")[0].set("descr", "错误码扣分表")
    graph = _prepare(docs.save(document))
    assert _ledger_of(graph).unit("docx:p[1]/img[0]").text == "[图片：错误码扣分表]"
    assert _ledger_of(graph).status("docx:p[1]/img[0]").status == "context"


def test_prefill_comes_from_word_evidence_and_conflicts_stay_unclaimed(requirement_items_on):
    graph = _prepare(fx.template_docx_with_rule_table())
    raw = graph["compilation"]["raw_parse_output"]
    ledger = _ledger_of(graph)
    prefilled = [item for item in raw["requirement_items"]["items"] if item["prefill"]]
    assert [item["prefill"] for item in prefilled] == [{"criterion_codes": ["C01"], "source": "word_evidence"}]
    assert prefilled[0]["unit_ids"][0] == "docx:tbl[0]/r1/c0"
    assert {item["type"] for item in raw["source_conflicts"]} == {"score_mismatch", "word_only"}
    # 冲突单元要由人处理，不能被章节或表头的标记盖掉
    for conflict in raw["source_conflicts"]:
        assert all(ledger.status(unit).status == "unclaimed" for unit in conflict["unit_ids"])


def test_toc_line_does_not_create_a_false_conflict(requirement_items_on):
    document = _document("创新性（5分）......3", "正文")
    raw = _prepare(docs.save(document))["compilation"]["raw_parse_output"]
    assert raw["source_conflicts"] == []


def test_unread_header_becomes_a_warning(requirement_items_on):
    document = _document("正文")
    document.sections[0].header.paragraphs[0].text = "页眉说明"
    warnings = _prepare(docs.save(document))["compilation"]["warnings"]
    assert [item["code"] for item in warnings if isinstance(item, dict) and item.get("code") == "TEMPLATE_READ_INCOMPLETE"]


def test_marking_stays_inside_the_word_document():
    """正文没有普通段落时，也不能把 Excel 单元当成 Word 单元去标记。"""

    document = Document()
    docs.add_sdt(document, "内容控件要求")
    ledger = SourceLedger()
    ledger.register(SourceUnit("xlsx:S!R1C1", "excel", "rules", "cell", "没有信号的文字", {}))
    view = load_docx(docs.save(document), ledger, doc_id="word", doc_role="template", deep=True)
    assert view.blocks == []
    mark_remaining_blocks(view, ledger)
    assert ledger.status("xlsx:S!R1C1").status == "unclaimed"


def _reparse(graph):
    compilation = graph["compilation"]
    return pipeline.prepare_structure_reparse(
        command=_command(),
        raw_parse_output=compilation["raw_parse_output"],
        artifacts=graph["artifacts"],
        structure_override=None,
    ).to_mapping()


def test_structure_reparse_keeps_items_and_marks(requirement_items_on, monkeypatch):
    document = _document("3.1 扣分规则......12", "四、必须遵守的规范", "创新性（5分）......3", "正文")
    docs.add_text_box(document.add_paragraph(""), "错误码缺失扣 2 分")
    graph = _prepare(docs.save(document))
    monkeypatch.setattr(settings, "REQUIREMENT_ITEMS_ENABLED", False)  # 已用新流程的草稿不看开关
    again = _reparse(graph)
    raw = again["compilation"]["raw_parse_output"]
    ledger = _ledger_of(again)
    before = graph["compilation"]["raw_parse_output"]
    assert raw["outline"] == before["outline"]
    assert raw["requirement_items"] == before["requirement_items"]
    assert ledger.status("docx:p[0]").status == "structural"
    assert ledger.status("docx:p[1]").status == "context"
    assert ledger.status("docx:p[4]/txbx[0]").status == "unclaimed"
    assert raw["source_conflicts"] == []


def test_structure_reparse_of_old_draft_stays_old():
    graph = _prepare(fx.template_docx_with_rule_table())
    again = _reparse(graph)
    assert "requirement_items" not in again["compilation"]["raw_parse_output"]


def test_ledger_payload_carries_the_new_keys():
    raw = {"source_ledger": {"units": []}, "outline": {"nodes": []}, "requirement_items": {"items": []}, "other": 1}
    assert parse_state.ledger_payload(raw) == {
        "source_ledger": {"units": []},
        "outline": {"nodes": []},
        "requirement_items": {"items": []},
    }


def test_assemble_copies_the_read_report():
    view, ledger = _read(docs.save(_document("正文")))
    payload = assemble_requirements(view, ledger)
    assert payload["requirement_items"]["read_report"] == view.read_report
    assert payload["requirement_items"]["read_report"] is not view.read_report


def test_import_api_stores_items_with_switch_on(client, requirement_items_on):
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": "规范", "version": "v1"},
        files={
            "rules_file": ("rules.xlsx", fx.simple_rules_xlsx(), XLSX),
            "template_file": ("spec.docx", docs.everything_docx(), DOCX),
        },
    )
    assert response.status_code == 200, response.text
    rubric_id = response.json()["rubric"]["id"]
    with client.session_factory() as session:
        raw = parse_state.current_compilation(session, rubric_id).raw_parse_output
    assert raw["requirement_items"]["items"]
    assert raw["outline"]["nodes"]
    coverage = client.get(f"/api/rubrics/{rubric_id}/parse-coverage")
    assert coverage.status_code == 200


def test_import_api_is_unchanged_with_switch_off(client):
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": "规范", "version": "v1"},
        files={
            "rules_file": ("rules.xlsx", fx.simple_rules_xlsx(), XLSX),
            "template_file": ("spec.docx", docs.everything_docx(), DOCX),
        },
    )
    assert response.status_code == 200, response.text
    rubric_id = response.json()["rubric"]["id"]
    with client.session_factory() as session:
        raw = parse_state.current_compilation(session, rubric_id).raw_parse_output
    assert "requirement_items" not in raw


# ---- 只读脚本 ----


def test_inspect_script_prints_outline_and_items(tmp_path, capsys):
    path = tmp_path / "模板.docx"
    path.write_bytes(docs.everything_docx())
    assert inspect_requirement_items.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "三、接口设计要求  (style, docx:p[8])" in output
    assert "检查项：错误码；要求：须统一编码；说明：按模块分段" in output
    assert "[目录] docx:p[0]" in output


def test_inspect_script_json_with_rules_goes_through_the_pipeline(tmp_path, capsys):
    template = tmp_path / "模板.docx"
    template.write_bytes(fx.template_docx_with_rule_table())
    rules = tmp_path / "评分表.xlsx"
    rules.write_bytes(fx.simple_rules_xlsx())
    assert inspect_requirement_items.main([str(template), "--rules", str(rules), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [item["prefill"]["criterion_codes"] for item in report["items"] if item["prefill"]] == [["C01"]]
    assert settings.REQUIREMENT_ITEMS_ENABLED is False  # 脚本用完要还原，不能影响同进程的其他导入
