"""L1 章节数字摘要（设计§7 L1）。

纯确定性统计，**不进任何 LLM prompt**（不改变评分/缓存键/QWK），仅供报告与人读、未来按需复用。
"""


def section_summaries(parsed):
    """返回 [{title, paragraphs, chars}]：逐章节段落数与字符数。"""
    summaries = []
    for section in parsed.get("sections") or []:
        paragraphs = section.get("paragraphs") or []
        texts = [p.get("text", "") if isinstance(p, dict) else str(p or "") for p in paragraphs]
        summaries.append(
            {
                "title": section.get("title") or "（未命名章节）",
                "paragraphs": len(paragraphs),
                "chars": sum(len(t) for t in texts),
            }
        )
    return summaries
