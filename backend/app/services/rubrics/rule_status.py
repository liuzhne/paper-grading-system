"""细则是否参与评分（评分细则排除落库改造方案 §3）。

老师排除的细则状态为 ``rejected``：它和它的审计留在库里，但不进入发布门禁的
可执行检查、版本内容哈希与评分快照。其余状态（``draft`` / ``review`` /
``approved``）都参与评分——未确认的照旧由门禁的 ``rule_not_approved`` 挡住。
"""

EXCLUDED_STATUS = "rejected"


def participates(rule) -> bool:
    return getattr(rule, "status", None) != EXCLUDED_STATUS


__all__ = ["EXCLUDED_STATUS", "participates"]
