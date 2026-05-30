"""docx 有效格式解析（设计§9 的硬骨头）。

.docx 的"有效格式"来自多层叠加：直接格式 < 段落/字符样式 < 样式继承 < docDefaults < theme 默认。
本模块直接解析 word/styles.xml（docDefaults + Normal 样式）与 word/theme/theme1.xml（中西文默认字体），
按 **Normal 样式 > docDefaults > theme** 求有效值；任何一层都拿不到 → 返回 None（**unknown 第三态**，
绝不臆测为某个具体值，更不据此判错）。

用途：对**模板**跑出"期望格式规格"（FormatSpec）作为基准；将来对**被评论文**跑出实际有效格式再比对。
"""

import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _q(namespace, tag):
    return "{%s}%s" % (namespace, tag)


def empty_spec():
    return {
        "body_font_ascii": None,
        "body_font_east_asian": None,
        "body_font_size_pt": None,
        "line_spacing": None,
        "source": "template",
    }


def resolve_default_format(source):
    """source 为 .docx 的 bytes 或路径。返回 FormatSpec（值为 None 表示无法确定）。"""
    data = source if isinstance(source, (bytes, bytearray)) else open(source, "rb").read()
    try:
        with zipfile.ZipFile(BytesIO(bytes(data))) as archive:
            names = archive.namelist()
            styles_xml = archive.read("word/styles.xml") if "word/styles.xml" in names else None
            theme_name = next((n for n in names if n.startswith("word/theme/theme") and n.endswith(".xml")), None)
            theme_xml = archive.read(theme_name) if theme_name else None
    except Exception:
        return empty_spec()
    return _resolve(styles_xml, theme_xml)


def _resolve(styles_xml, theme_xml):
    """纯函数：从 styles.xml / theme1.xml 字节求有效默认格式。便于单测 unknown 态。"""
    spec = empty_spec()
    normal_rpr = normal_ppr = docdef_rpr = docdef_ppr = None

    if styles_xml:
        try:
            root = ET.fromstring(styles_xml)
            docdef_rpr = root.find("./%s/%s/%s" % (_q(W, "docDefaults"), _q(W, "rPrDefault"), _q(W, "rPr")))
            docdef_ppr = root.find("./%s/%s/%s" % (_q(W, "docDefaults"), _q(W, "pPrDefault"), _q(W, "pPr")))
            for style in root.findall(_q(W, "style")):
                if style.get(_q(W, "styleId")) == "Normal":
                    normal_rpr = style.find(_q(W, "rPr"))
                    normal_ppr = style.find(_q(W, "pPr"))
                    break
        except ET.ParseError:
            pass

    theme_latin, theme_east_asian = _theme_fonts(theme_xml)

    normal_ascii, normal_ea = _fonts(normal_rpr)
    docdef_ascii, docdef_ea = _fonts(docdef_rpr)
    spec["body_font_ascii"] = normal_ascii or docdef_ascii or theme_latin
    spec["body_font_east_asian"] = normal_ea or docdef_ea or theme_east_asian
    # theme 不定义字号；字号只能来自 Normal 或 docDefaults。
    spec["body_font_size_pt"] = _size_pt(normal_rpr)
    if spec["body_font_size_pt"] is None:
        spec["body_font_size_pt"] = _size_pt(docdef_rpr)
    spec["line_spacing"] = _line_spacing(normal_ppr)
    if spec["line_spacing"] is None:
        spec["line_spacing"] = _line_spacing(docdef_ppr)
    return spec


def _fonts(rpr):
    if rpr is None:
        return (None, None)
    rfonts = rpr.find(_q(W, "rFonts"))
    if rfonts is None:
        return (None, None)
    return (rfonts.get(_q(W, "ascii")), rfonts.get(_q(W, "eastAsia")))


def _size_pt(rpr):
    if rpr is None:
        return None
    size = rpr.find(_q(W, "sz"))
    if size is None:
        return None
    value = size.get(_q(W, "val"))
    if not value:
        return None
    try:
        return float(value) / 2  # w:sz 单位是半磅
    except ValueError:
        return None


def _line_spacing(ppr):
    """只在 lineRule=auto（倍数行距）时换算为倍数；exact/atLeast（固定磅值）无法表为倍数 → None。"""
    if ppr is None:
        return None
    spacing = ppr.find(_q(W, "spacing"))
    if spacing is None:
        return None
    line = spacing.get(_q(W, "line"))
    rule = spacing.get(_q(W, "lineRule"))
    if not line:
        return None
    if rule not in (None, "auto"):
        return None
    try:
        return round(float(line) / 240, 2)  # 240 = 单倍行距
    except ValueError:
        return None


def _theme_fonts(theme_xml):
    if not theme_xml:
        return (None, None)
    try:
        root = ET.fromstring(theme_xml)
    except ET.ParseError:
        return (None, None)
    minor = root.find(".//%s/%s" % (_q(A, "fontScheme"), _q(A, "minorFont")))
    if minor is None:
        return (None, None)
    latin_el = minor.find(_q(A, "latin"))
    latin = latin_el.get("typeface") if latin_el is not None else None
    east_asian = None
    for font in minor.findall(_q(A, "font")):
        if font.get("script") == "Hans":
            east_asian = font.get("typeface")
            break
    return (latin or None, east_asian or None)
