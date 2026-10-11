"""评分助手的纯函数：论文定位、失败诊断、分数概览、文案。

“下一步做什么”由代码按服务器状态推出，提问与汇报用这里的模板生成，不调用模型
（方案 §4、§6）。这里不读写数据库，便于单测。
"""

from __future__ import annotations

from datetime import datetime

#: 单个登录用户通过助手一次最多提交的文件数（U2、B5）。
MAX_FILES = 30

HELP_TEXT = "\n".join([
    "我可以帮你完成整个评分：",
    "· 上传待评分的文件（一次最多 30 份），默认用最新发布的评分标准评分；",
    "· 用你自己的评分规则和模板：导入后在右侧核对并发布，发布后接着评分；",
    "· 查询进度、每篇的得分、扣分点和证据、需要复核的内容、整体分数情况；",
    "· 评分出错时说明原因，临时性错误会自动重试一次。",
    "评分过程的每一步都显示在右侧的页面上。改分请在评分工作区里完成。",
])

#: 快捷按钮：发出一句规则能识别的话，与自由输入走同一条路径。
QUICK_ACTIONS = {
    "start_grading": ("开始评分", "开始评分"),
    "import_rubric": ("用我的评分规则和模板", "上传我的评分规则和模板"),
    "query_progress": ("评分进度", "评分进度"),
    "query_paper": ("查某篇的扣分", "查询扣分"),
    "query_review": ("需要复核的", "哪些需要复核"),
    "query_overview": ("整体情况", "整体分数情况"),
}


def quick_actions_card(intents: list[str] | None = None) -> dict:
    keys = intents or list(QUICK_ACTIONS)
    return {
        "type": "quick_actions",
        "actions": [{"label": QUICK_ACTIONS[key][0], "text": QUICK_ACTIONS[key][1]} for key in keys],
    }


def batch_name(now: datetime, count: int) -> str:
    return "助手评分 · %02d-%02d %02d:%02d · %d 份" % (now.month, now.day, now.hour, now.minute, count)


def papers_in_upload_order(papers: list[dict]) -> list[dict]:
    """批次汇总按上传时间倒序返回；“第 N 篇”按上传顺序数。"""

    return list(reversed(papers or []))


def _stem(file_name: str) -> str:
    name = str(file_name or "")
    return name.rsplit(".", 1)[0] if "." in name else name


def resolve_paper(papers: list[dict], *, ordinal: int | None = None, name: str | None = None, text: str = ""):
    """定位用户说的是哪一篇：序号 → 姓名或文件名片段 → 原话里出现的姓名或文件名。

    唯一命中才算定位成功，返回 (论文, 候选)；否则返回 (None, 候选) 让用户选，不猜。
    """

    if ordinal is not None:
        if 1 <= ordinal <= len(papers):
            match = papers[ordinal - 1]
            return match, [match]
        return None, papers
    name = (name or "").strip()
    if name:
        hits = [p for p in papers if name in (p.get("student_name") or "") or name in _stem(p.get("file_name"))]
        if len(hits) == 1:
            return hits[0], hits
        if hits:
            return None, hits
    if text:
        hits = []
        for paper in papers:
            student = (paper.get("student_name") or "").strip()
            stem = _stem(paper.get("file_name")).strip()
            if (len(student) >= 2 and student in text) or (len(stem) >= 2 and stem in text):
                hits.append(paper)
        if len(hits) == 1:
            return hits[0], hits
        if hits:
            return None, hits
    return None, papers


def normalize_error_code(code: str | None) -> str:
    """条目错误码有三种来源（规则任务的 PROVIDER_* 根因、厂商错误的小写码、兜底的启发式码），统一后再判断。"""

    value = str(code or "UNKNOWN").strip().upper()
    return value[len("PROVIDER_"):] if value.startswith("PROVIDER_") else value


#: 临时性错误：重试只重跑失败的那几篇，已成功的结果复用，不影响评分结果（U4、B3）。
TRANSIENT_CODES = frozenset({
    "RATE_LIMITED", "REQUEST_TIMEOUT", "TIMEOUT", "CAPACITY_UNAVAILABLE",
    "PROVIDER_UNAVAILABLE", "NETWORK_ERROR", "CIRCUIT_OPEN", "CONFLICT",
})

DIAGNOSES = {
    "RATE_LIMITED": ("模型限流", "模型服务暂时拒绝了过多请求，稍后重试即可；若反复出现，可在账户页调低连接的“同时请求数”。"),
    "REQUEST_TIMEOUT": ("请求超时", "模型响应超时，属于临时性问题，重试即可。"),
    "TIMEOUT": ("请求超时", "模型响应超时，属于临时性问题，重试即可。"),
    "CAPACITY_UNAVAILABLE": ("模型服务繁忙", "模型服务容量不足，稍后重试即可。"),
    "PROVIDER_UNAVAILABLE": ("模型服务不可用", "模型服务暂时不可用，稍后重试即可。"),
    "NETWORK_ERROR": ("网络错误", "连接模型服务时网络出错，稍后重试即可。"),
    "CIRCUIT_OPEN": ("连续失败后暂停调用", "同一连接连续失败后系统暂停调用一段时间，稍后重试即可。"),
    "OUTPUT_TRUNCATED": (
        "模型输出被截断",
        "模型输出达到输出 token 上限被截断。需要在账户页调大该连接的输出上限（或降低思考强度），"
        "然后重新建一个评分任务——改了连接配置后，原任务的重试会被拒绝。",
    ),
    "QUOTA_EXHAUSTED": ("额度用完", "该连接的额度已用完，重试解决不了；请充值或换一条连接后重新建任务。"),
    "AUTHENTICATION_FAILED": ("密钥无效", "模型连接的密钥无效或已过期，请在账户页更新后重新建任务。"),
    "PERMISSION_DENIED": ("无权使用该模型", "该密钥无权调用这个模型，请在账户页检查连接配置。"),
    "MODEL_OR_ENDPOINT_NOT_FOUND": ("模型或地址不存在", "连接里的模型名或接口地址不正确，请在账户页检查。"),
    "OUTPUT_REFUSED": ("模型拒答", "模型按其安全策略拒绝评判部分规则，这些规则需要人工复核。"),
    "AI_CONNECTION_CONFIG_CHANGED": ("连接配置已变更", "任务创建后连接配置被修改，原任务不能继续；请重新建一个评分任务。"),
    "AI_CONNECTION_KEY_CHANGED": ("连接密钥已变更", "任务创建后连接密钥被轮换，原任务不能继续；请重新建一个评分任务。"),
    "TOKEN_BUDGET_EXCEEDED": ("单篇输入超过上限", "这篇论文的输入超过了单篇 token 上限，需要管理员调高上限后重试。"),
    "RULE_EXECUTION_FAILED": ("系统错误", "评分规则执行失败，属于系统问题，已记录；请把任务名称告诉管理员。"),
}


def diagnose_job(items: list[dict], papers_by_id: dict[str, dict]) -> dict:
    """按根因码把失败条目分组；`items` 为任务条目（paper_id、status、error_code、error_message）。"""

    groups: dict[str, dict] = {}
    failed = [item for item in items if item.get("status") == "failed"]
    for item in failed:
        code = normalize_error_code(item.get("error_code"))
        label, advice = DIAGNOSES.get(code, ("评分失败", item.get("error_message") or "评分失败，原因未记录。"))
        group = groups.setdefault(code, {
            "code": code, "label": label, "advice": advice, "count": 0, "file_names": [],
            "transient": code in TRANSIENT_CODES,
        })
        group["count"] += 1
        if len(group["file_names"]) < 10:
            group["file_names"].append((papers_by_id.get(item.get("paper_id")) or {}).get("file_name") or "材料")
    ordered = sorted(groups.values(), key=lambda group: -group["count"])
    return {
        "failed_count": len(failed),
        "groups": ordered,
        "transient_only": bool(ordered) and all(group["transient"] for group in ordered),
    }


def score_overview(papers: list[dict]) -> dict:
    """分数概览：只用批次汇总里的现成分数，不经模型（B8）。"""

    scores = [p.get("latest_final_score") for p in papers if isinstance(p.get("latest_final_score"), (int, float))]
    rounded = lambda value: round(value, 2)  # noqa: E731 - 与分数本身同精度
    return {
        "total": len(papers),
        "scored": len(scores),
        "average": rounded(sum(scores) / len(scores)) if scores else None,
        "max": max(scores) if scores else None,
        "min": min(scores) if scores else None,
        "need_review": sum(1 for p in papers if p.get("latest_need_manual_review")),
    }


def overview_text(overview: dict) -> str:
    if not overview["scored"]:
        return "共 %d 份，还没有评分结果。" % overview["total"]
    parts = [
        "共 %d 份，已出分 %d 份" % (overview["total"], overview["scored"]),
        "平均 %s 分" % overview["average"],
        "最高 %s 分" % overview["max"],
        "最低 %s 分" % overview["min"],
    ]
    if overview["need_review"]:
        parts.append("需要复核 %d 份" % overview["need_review"])
    return "，".join(parts) + "。"


REMOVABLE_CODES = ("parse_failed", "scanned_document")


def blocking_findings(precheck: dict) -> tuple[list[dict], list[dict]]:
    """预检里的阻断项：只有解析失败（含扫描件）的材料可以移除；“尚未解析”不行。"""

    blocking = [f for f in (precheck or {}).get("findings", []) if f.get("severity") == "blocking"]
    removable = [f for f in blocking if f.get("code") in REMOVABLE_CODES]
    stuck = [f for f in blocking if f.get("code") not in REMOVABLE_CODES]
    return removable, stuck
