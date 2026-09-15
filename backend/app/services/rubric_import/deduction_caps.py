"""Separate AI mutually exclusive group limits from per-rule repeat limits."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping


def normalize_ai_group_caps(rows, *, criterion_code, maximum):
    """Normalize only attributable single-hit AI groups, including old clients.

    A group cap is redundant at execution only when every member is single-hit,
    shares the mutex, and each deduction fits the group limit. Keep that limit
    in the criterion projection and original source, never in AtomicRule.cap.
    Unknown/manual repeat caps must still reach publication validation intact.
    """
    result = deepcopy(rows)
    groups = {}
    for row in result:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("draft_row_key") or "").split("::")
        fingerprint = row.get("generation_fingerprint")
        if len(key) == 3 and isinstance(fingerprint, str):
            groups.setdefault((fingerprint, key[0], key[1]), []).append(row)
    for (fingerprint, code, group), members in groups.items():
        valid = bool(fingerprint and code == criterion_code and group)
        mutexes, caps = set(), set()
        for row in members:
            metadata = row.get("generation_metadata")
            valid &= (str(row.get("source") or "").startswith("ai_")
                      and isinstance(metadata, Mapping)
                      and metadata.get("fingerprint") == fingerprint
                      and row.get("repeat_policy") == "once"
                      and bool(row.get("mutex_group"))
                      and not row.get("levels"))
            try:
                cap = Decimal(str(row.get("group_cap_points", row.get("cap_points"))))
                points = Decimal(str(row.get("points")))
                limit = Decimal(str(maximum))
                valid &= all(n.is_finite() for n in (cap, points, limit)) and 0 < points <= cap <= limit
                # Explicit group and individual limits may not disagree.
                if "group_cap_points" in row and row.get("cap_points") is not None:
                    valid &= Decimal(str(row["cap_points"])) == cap
                caps.add(cap)
                mutexes.add(row.get("mutex_group"))
            except (InvalidOperation, ValueError, TypeError):
                valid = False
        valid &= len(caps) == 1 and len(mutexes) == 1
        if valid:
            for row in members:
                row["group_cap_points"] = row.get("group_cap_points", row.get("cap_points"))
                row["cap_points"] = None
        elif any("group_cap_points" in row for row in members):
            raise ValueError(f"{criterion_code} AI 规则组上限无法核验，请检查单次命中、互斥组、来源和分值范围")
    # Explicit new-format group caps without attributable group identity fail closed.
    if any(isinstance(row, Mapping) and "group_cap_points" in row and row.get("cap_points") is not None for row in result):
        raise ValueError(f"{criterion_code} AI 规则组上限与单条累计上限冲突")
    for row in result:
        if isinstance(row, Mapping) and "group_cap_points" in row:
            key = str(row.get("draft_row_key") or "").split("::")
            if len(key) != 3 or not isinstance(row.get("generation_fingerprint"), str):
                raise ValueError(f"{criterion_code} AI 规则组缺少可追溯编号")
    return result
