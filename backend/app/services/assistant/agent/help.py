"""帮助条目（Agent 化方案 §4、§14）。

由代码维护，模型只负责选用：回答“怎么用”时，模型调用 `help_lookup` 找到条目，在 `reply` 的 `help_refs`
里引用，界面显示的是这里的固定文案——保证对按钮、页面的描述准确，模型编不出不存在的按钮。
文案里的按钮名与界面一致（`AssistantCard.vue`、`AssistantView.vue`），改界面时同步改这里。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.assistant.agent import catalog


@dataclass(frozen=True)
class HelpEntry:
    key: str
    title: str
    text: str
    keywords: tuple[str, ...]


ENTRIES: tuple[HelpEntry, ...] = (
    HelpEntry("assistant.capabilities", "助手能做什么",
              "我可以帮你开始评分、导入自己的评分规则和模板，查询评分进度、每份的得分与扣分点、需要复核的内容和整体情况，"
              "失败时说明原因并提议重试。我不打分、不改分，改动数据的操作都要你在卡片上确认。",
              ("能做什么", "功能", "帮助", "你是谁", "介绍")),
    HelpEntry("grading.start", "怎么开始评分",
              "说“开始评分”或点输入框左边的附件按钮，选择待评分的文件（.docx 或 .pdf，一次最多 30 份）。"
              "确认卡片上会写明用哪份评分标准、哪种文档类型和评分模型；点“开始评分”才会建任务并开始，"
              "也可以点“换评分标准”“重新选择文件”或“不评了”。",
              ("开始评分", "上传论文", "上传文件", "选文件", "评一下", "附件")),
    HelpEntry("grading.progress", "怎么看评分进度",
              "评分在后台进行，右侧会打开“评分进度”页；评完我会在对话里汇报结果。页面隐藏时进度暂停刷新，回到页面就会继续。",
              ("进度", "评完", "多久", "评到哪", "等待")),
    HelpEntry("rubric.import", "怎么用自己的评分规则和模板",
              "说“上传我的评分规则和模板”，在出现的卡片里填“评分标准名称”，选“文档类型”（上传后不能再改），"
              "再选“评分规则文件”和“评分模板文件”（至少一份），点“导入”。导入后在右侧核对并发布，发布后就能用它评分。",
              ("导入", "评分规则", "评分模板", "模板怎么上传", "上传模板", "自己的标准")),
    HelpEntry("rubric.difference", "评分标准、评分规则与评分模板的区别",
              "评分标准是系统里用来评分的那一份（有名称和版本，发布后才能用）；评分规则文件写的是各评分项怎么给分、怎么扣分，"
              "评分模板文件是评分表的格式。导入时两种文件至少上传一种，系统据此生成评分标准，由你核对后发布。",
              ("区别", "评分标准", "评分规则", "评分模板", "是什么")),
    HelpEntry("rubric.publish", "评分标准怎么发布",
              "评分标准只能由你在评分标准页核对后发布，助手不能代为发布。导入后右侧会打开这份标准，核对无误后在页面上发布。",
              ("发布", "核对", "审核", "生效")),
    HelpEntry("review.manual", "什么是人工复核、在哪做",
              "证据不足、模型输出异常或分差需要确认的评分项会标为“需要人工复核”。复核在右侧的评分工作区完成：点开某一份，"
              "逐项查看原文证据，确认或改分。",
              ("复核", "人工确认", "待确认", "需要确认")),
    HelpEntry("score.edit", "怎么改分",
              "改分在评分工作区完成：打开那一份，在对应评分项上改分并写明原因，改分会记入复核记录。助手现在不能直接改分。",
              ("改分", "修改分数", "调分", "给分不对")),
    HelpEntry("retry.failed", "评分失败了怎么办",
              "临时性错误（例如模型限流、超时）会自动重试一次。仍然失败的，可以说“重试失败项”，确认后只重跑失败的那几份，"
              "已成功的结果保留。反复失败时卡片上会写明原因和处理办法。",
              ("失败", "重试", "出错", "报错", "没评完")),
    HelpEntry("model.setup", "助手用哪个模型、怎么换",
              "助手默认用你自己启用的模型连接，没有时用平台模型（会比较慢）。左下角“助手模型设置”可以更换。"
              "评分用的模型由评分任务决定，与助手模型无关。",
              ("模型", "换模型", "助手模型", "连接", "太慢")),
    HelpEntry("files.formats", "支持哪些文件",
              "待评分的文件支持 .docx 和 .pdf，一次最多 30 份。解析失败的文件会在开始前列出，可以移除后再评。",
              ("格式", "docx", "pdf", "多少份", "文件类型")),
)

BY_KEY = {entry.key: entry for entry in ENTRIES}


def lookup(topic: str, *, limit: int = 3) -> list[HelpEntry]:
    """按条目键或关键词找帮助条目：键完全一致优先，其余按词重合度排序。"""

    topic = str(topic or "").strip()
    if topic in BY_KEY:
        return [BY_KEY[topic]]
    terms = set(catalog.tokenize(topic))
    scored = []
    for position, entry in enumerate(ENTRIES):
        words = set(catalog.tokenize(" ".join([entry.title, *entry.keywords])))
        overlap = len(terms & words)
        if overlap:
            scored.append((-overlap, position, entry))
    scored.sort()
    return [entry for _overlap, _position, entry in scored[:limit]]


__all__ = ["BY_KEY", "ENTRIES", "HelpEntry", "lookup"]
