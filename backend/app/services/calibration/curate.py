"""L2 锚点策展（设计§7/§15.3）：从已评论文按教师分**各档**选锚点 + 脱敏 + 剔除留出集。

纯选取/脱敏逻辑在此（可单测、无 PII、不调 LLM）；真正落库的编排见 `scripts/build_anchors.py`（读真实
论文、抽取评分项相关段落、写 CalibrationAnchor，数据放仓库外、本地跑）。

两条硬约束：
- 锚点用**脱敏范文**（去学生姓名/学号等质量无关属性，§15.3）。
- 锚点论文**必须从留出集剔除**，否则评估泄漏（拿被评样本当范例＝作弊）。
"""

import hashlib

# 分位目标：取近极值（0.05/0.5/0.95）覆盖 差/中/优，最大化 few-shot 对比度——
# 教师逐项真值方差很小（stdev 1~2.6），保守分位会让低/中/高锚点挨太近、教不动模型。
# （get_anchors 注入上限 4，取 3 不溢出。）
DEFAULT_LEVELS = [("低", 0.05), ("中", 0.50), ("高", 0.95)]


def select_anchor_targets(rows, holdout_filenames, criterion_codes, levels=None, max_anchors=3):
    """为每个评分项选锚点论文：候选=非留出集且该项有分；按分位目标取最接近的论文。

    返回 {code: [{filename, score, level}]}。确定性：同输入恒同输出（hash 稳定 tie-break，可复现）。
    """
    levels = (levels or DEFAULT_LEVELS)[:max_anchors]
    holdout = set(holdout_filenames or [])
    result = {}
    for code in criterion_codes:
        pool = [
            (row["filename"], float(row["items"][code]))
            for row in rows
            if row.get("filename") not in holdout
            and code in (row.get("items") or {})
            and row["items"][code] is not None
        ]
        if not pool:
            result[code] = []
            continue
        scores = sorted(score for _, score in pool)
        picked = []
        used = set()
        for label, quantile in levels:
            target = _quantile(scores, quantile)
            for filename, score in sorted(pool, key=lambda item: (abs(item[1] - target), _hash_frac(item[0]))):
                if filename not in used:
                    used.add(filename)
                    picked.append({"filename": filename, "score": score, "level": label})
                    break
        result[code] = picked
    return result


def desensitize(text, names=None, ids=None):
    """删除质量无关属性（学生姓名/学号），替换为占位「某」（§15.3 公平/隐私）。"""
    cleaned = text or ""
    for token in list(names or []) + list(ids or []):
        token = (str(token) if token is not None else "").strip()
        if len(token) >= 2:  # 过短的 token 误伤正文，跳过
            cleaned = cleaned.replace(token, "某")
    return cleaned


def anchor_rationale(criterion_name, score, max_score, level):
    """锚点理由模板（人可后续精修）：传达"教师给该项多少分 + 属哪一档"的校准信号。"""
    return "教师对「%s」该项给 %g/%g（%s档范例）。请据此对齐宽严尺度。" % (
        criterion_name, float(score), float(max_score), level,
    )


def _hash_frac(key):
    return int(hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:8], 16) / 0x100000000


def _quantile(sorted_scores, q):
    if not sorted_scores:
        return 0.0
    idx = q * (len(sorted_scores) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_scores) - 1)
    frac = idx - lo
    return sorted_scores[lo] * (1 - frac) + sorted_scores[hi] * frac
