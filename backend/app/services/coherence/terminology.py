"""术语一致性聚类（设计§8 篇章一致性·确定性切片）。

学位论文要求术语前后统一。本模块用**确定性**手段聚类术语的多种写法并核对：

1. 缩写定义聚类：抽取「全称（ABBR）」定义对，聚成 abbr↔全称 簇——
   - 同一缩写对应多个不同全称（定义打架）→ warning；
   - 同一全称对应多个不同缩写 → warning。
2. 英文术语写法聚类：把同一术语的大小写/写法变体（如 Transformer/transformer）聚为一簇，
   一簇有多种写法 → info（提示统一写法，不判错）。

语义级的近义术语归并（需向量/LLM）留待 §8 语义半增强；此处只做可确定判定的部分。
"""

import re

# 全称（ABBR） / 全称(ABBR)：缩写须含至少一个大写字母，避免把普通括号英文误判为缩写。
_DEF_RE = re.compile(r"([一-龥A-Za-z0-9]{2,30})[（(]([A-Za-z][A-Za-z0-9\-]{1,15})[）)]")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")

# 常见非术语英文词，排除以降噪。
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "are", "was", "were",
    "http", "https", "www", "com", "org", "etc", "eg", "ie",
}


def analyze_terminology(full_text):
    """返回术语一致性 findings；无问题或空文本 → []。"""
    text = full_text or ""
    return _abbreviation_findings(text) + _variant_findings(text)


def _abbreviation_findings(text):
    # 正文里全称前常黏连上文（如「但有的章节写支持向量机」），正则会过度捕获前缀。
    # 归一化：若某全称是另一全称的后缀，视为同一术语（取最短后缀为规范形），避免漏/误判。
    raw_pairs = [(full, abbr) for full, abbr in _DEF_RE.findall(text) if re.search(r"[A-Z]", abbr)]
    all_fulls = {full for full, _ in raw_pairs}

    abbr_to_fulls = {}
    full_to_abbrs = {}
    for full, abbr in raw_pairs:
        canonical = _canonical_full(full, all_fulls)
        abbr_to_fulls.setdefault(abbr, set()).add(canonical)
        full_to_abbrs.setdefault(canonical, set()).add(abbr)

    findings = []
    for abbr, fulls in sorted(abbr_to_fulls.items()):
        if len(fulls) >= 2:
            findings.append(
                _finding(
                    "terminology_abbr_conflict",
                    "warning",
                    "缩写「%s」对应多个全称：%s（术语定义不一致）" % (abbr, "、".join(sorted(fulls))),
                    refs=sorted(fulls),
                )
            )
    for full, abbrs in sorted(full_to_abbrs.items()):
        if len(abbrs) >= 2:
            findings.append(
                _finding(
                    "terminology_full_conflict",
                    "warning",
                    "术语「%s」对应多个缩写：%s（缩写不统一）" % (full, "、".join(sorted(abbrs))),
                    refs=sorted(abbrs),
                )
            )
    return findings


def _canonical_full(full, all_fulls):
    """取 full 的最短后缀规范形：在所有观察到的全称里，找能作为 full 后缀的最短者。"""
    candidates = [other for other in all_fulls if full.endswith(other)]
    return min(candidates, key=len) if candidates else full


def _variant_findings(text):
    counts = {}
    for token in _TOKEN_RE.findall(text):
        counts[token] = counts.get(token, 0) + 1

    clusters = {}
    for surface, count in counts.items():
        if surface.lower() in _STOPWORDS:
            continue
        clusters.setdefault(surface.lower(), {})[surface] = count

    findings = []
    for key, surfaces in sorted(clusters.items()):
        if len(surfaces) < 2:
            continue
        total = sum(surfaces.values())
        if total < 3:  # 出现太少不提示，降噪
            continue
        ordered = sorted(surfaces, key=lambda s: (-surfaces[s], s))
        findings.append(
            _finding(
                "terminology_variant",
                "info",
                "术语写法不统一：%s（建议统一为一种写法）" % " / ".join(ordered),
                refs=ordered,
            )
        )
    return findings


def _finding(kind, severity, message, refs=None, location=None):
    return {"kind": kind, "severity": severity, "message": message, "refs": refs or [], "location": location}
