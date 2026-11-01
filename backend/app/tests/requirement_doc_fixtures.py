"""Word 模板读取和条目组装用的合成文档（无真实 PII）。"""

from base64 import b64decode
from io import BytesIO
from random import Random

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
V = "urn:schemas-microsoft-com:vml"


def save(document) -> bytes:
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def add_outline_paragraph(document, text, level=0):
    paragraph = document.add_paragraph(text)
    paragraph._p.get_or_add_pPr().append(parse_xml(f'<w:outlineLvl {nsdecls("w")} w:val="{level}"/>'))
    return paragraph


def add_inserted_text(paragraph, text, *, revision=9):
    paragraph._p.append(
        parse_xml(f'<w:ins {nsdecls("w")} w:id="{revision}" w:author="r"><w:r><w:t>{text}</w:t></w:r></w:ins>')
    )


def add_deleted_text(paragraph, text, *, revision=19):
    paragraph._p.append(
        parse_xml(f'<w:del {nsdecls("w")} w:id="{revision}" w:author="r"><w:r><w:delText>{text}</w:delText></w:r></w:del>')
    )
    paragraph._p.append(
        parse_xml(f'<w:moveFrom {nsdecls("w")} w:id="{revision + 1}" w:author="r"><w:r><w:t>{text}</w:t></w:r></w:moveFrom>')
    )


def add_text_box(paragraph, text):
    """和 Word 一样在 Choice 和 Fallback 里各写一份。"""

    paragraph._p.append(
        parse_xml(
            f'<w:r {nsdecls("w")} xmlns:mc="{MC}" xmlns:wps="{WPS}" xmlns:v="{V}"><mc:AlternateContent>'
            f'<mc:Choice Requires="wps"><w:drawing><wps:txbx><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
            "</w:txbxContent></wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>"
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
            "</mc:AlternateContent></w:r>"
        )
    )


def add_sdt(document, *texts, gallery=None):
    properties = (
        f'<w:sdtPr><w:docPartObj><w:docPartGallery w:val="{gallery}"/></w:docPartObj></w:sdtPr>' if gallery else ""
    )
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in texts)
    element = parse_xml(f'<w:sdt {nsdecls("w")}>{properties}<w:sdtContent>{body}</w:sdtContent></w:sdt>')
    document.element.body.insert(len(document.element.body) - 1, element)
    return element


def fill_table(document, rows):
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for row_index, values in enumerate(rows):
        for col_index, value in enumerate(values):
            if value:
                table.cell(row_index, col_index).text = value
    return table


def everything_docx() -> bytes:
    """每种要读的结构各来一份。"""

    document = Document()
    add_sdt(document, "一、需求分析要求 扣分说明..........1", gallery="Table of Contents")
    document.add_paragraph("3.1 扣分规则......12")
    document.add_paragraph("一、需求分析要求")
    document.add_paragraph("需求分析须覆盖业务范围，明确验收标准，并列出非功能需求。")
    document.add_paragraph("二、总体架构要求")
    document.add_paragraph("总体架构须包括以下内容：")
    document.add_paragraph("1、分层设计")
    document.add_paragraph("2、部署拓扑")
    document.add_paragraph("3、关键技术选型的理由")
    document.add_heading("三、接口设计要求", level=1)
    document.add_paragraph("接口文档应给出错误码说明。")
    table = fill_table(
        document,
        [("检查项", "要求", "说明"), ("错误码", "须统一编码", "按模块分段"), ("", "须有中文说明", "—"), ("鉴权", "不得明文传输密钥", "—")],
    )
    table.cell(1, 0).merge(table.cell(2, 0))
    table.cell(1, 0).text = "错误码"
    commented = document.add_paragraph("性能指标须写明")
    document.add_comment(commented.runs, text="这里必须写明并发量和响应时间", author="reviewer")
    document.add_paragraph("四、部署要求")
    document.add_paragraph("部署方案须包括：")
    document.add_paragraph("主备切换", style="List Number")
    document.add_paragraph("回滚步骤", style="List Number")
    add_outline_paragraph(document, "五、安全要求")
    tracked = document.add_paragraph("密钥须定期轮换")
    add_inserted_text(tracked, "，周期不超过 90 天")
    add_deleted_text(tracked, "已删除的文字")
    add_inserted_text(document.add_paragraph(), "整段都是修订插入：须保留审计日志", revision=11)
    add_text_box(document.add_paragraph("正文和文本框同段"), "文本框要求：必须写明并发量")
    add_sdt(document, "内容控件要求：须提供部署拓扑")
    document.add_paragraph("架构图示例：")
    document.add_picture(BytesIO(PNG))
    document.add_paragraph("图 1 系统架构示意")
    mixed = document.add_paragraph("图文混排：架构须如下图")
    mixed.add_run().add_picture(BytesIO(PNG))
    document.add_picture(BytesIO(PNG))
    outer = document.add_table(rows=3, cols=2)
    outer.cell(0, 0).text = "项目"
    outer.cell(0, 1).text = "内容"
    outer.cell(1, 0).text = "编码"
    outer.cell(1, 1).text = "外层单元格"
    outer.cell(1, 1).add_table(rows=1, cols=1).cell(0, 0).text = "嵌套表格要求：须统一编码"
    outer.cell(2, 0).text = "配图"
    outer.cell(2, 1).paragraphs[0].add_run().add_picture(BytesIO(PNG))
    bold = document.add_paragraph()
    bold.add_run("交付物清单").bold = True
    document.add_paragraph("需提交源码、文档和测试报告。")
    document.add_paragraph("总分 100 分")
    document.add_paragraph("3")
    return save(document)


_PARAGRAPHS = (
    "接口文档应给出错误码说明。",
    "分层设计",
    "扣分规则",
    "总分 100 分",
    "12",
    "图 1 架构示意",
    "按下列要求编写：",
    "一、总体要求",
    "（一）范围",
    "1.1 系统目标",
    "第二章 设计",
    "1、接口规范",
    "2、部署拓扑",
    "（1）主备切换",
    "① 回滚步骤",
    "• 监控告警",
    "错误码缺失扣 2 分。",
)


def random_docx(seed: int, *, revisions: bool = True) -> bytes:
    """随机拼装的文档序列，用来测试每个单元只属于一处、条目顺序和文档顺序一致。"""

    random = Random(seed)
    document = Document()
    comment_budget = 3
    for _ in range(random.randint(3, 25)):
        kind = random.choice(
            ("text", "text", "text", "text", "style", "numbered", "bold", "outline", "image", "mixed", "box", "sdt",
             "toc", "ins", "table", "merged", "nested", "empty")
        )
        text = random.choice(_PARAGRAPHS)
        if kind == "text":
            paragraph = document.add_paragraph(text)
            if comment_budget and random.random() < 0.2:
                document.add_comment(paragraph.runs, text=f"批注 {comment_budget}", author="r")
                comment_budget -= 1
        elif kind == "style":
            document.add_heading(text, level=random.randint(1, 3))
        elif kind == "numbered":
            for _ in range(random.randint(1, 11)):
                document.add_paragraph(random.choice(_PARAGRAPHS), style="List Number")
        elif kind == "bold":
            document.add_paragraph().add_run(text).bold = True
        elif kind == "outline":
            add_outline_paragraph(document, text, random.randint(0, 2))
        elif kind == "image":
            document.add_picture(BytesIO(PNG))
        elif kind == "mixed":
            document.add_paragraph(text).add_run().add_picture(BytesIO(PNG))
        elif kind == "box":
            add_text_box(document.add_paragraph(random.choice(("", text))), "文本框要求")
        elif kind == "sdt":
            add_sdt(document, text, "内容控件第二段")
        elif kind == "toc":
            document.add_paragraph(f"{text}......{random.randint(1, 9)}")
        elif kind == "ins":
            if revisions:
                add_inserted_text(document.add_paragraph(), text, revision=random.randint(100, 999))
            else:
                document.add_paragraph(text)
        elif kind == "empty":
            document.add_paragraph("")
        else:
            rows = random.randint(1, 4)
            cols = random.randint(1, 3)
            table = document.add_table(rows=rows, cols=cols)
            for row in range(rows):
                for col in range(cols):
                    if random.random() < 0.8:
                        table.cell(row, col).text = random.choice(_PARAGRAPHS)
            if kind == "merged" and rows > 1:
                table.cell(0, 0).merge(table.cell(1, 0))
            if kind == "nested":
                cell = table.cell(rows - 1, cols - 1)
                cell.add_table(rows=1, cols=2).cell(0, 1).text = random.choice(_PARAGRAPHS)
                if random.random() < 0.5:
                    cell.paragraphs[0].add_run().add_picture(BytesIO(PNG))
    return save(document)
