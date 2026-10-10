"""Recover display-only provenance from the manual compiler's explicit row identity."""
import re


AI_RULE_SOURCES = frozenset({"ai_interpreted_user_text", "ai_inferred", "llm"})


def rule_origin(rule, criterion):
    entries = criterion.deduction_rules_structured or []
    explicit = next((item for item in entries if item.get("rule_code") == rule.rule_code), None)
    if explicit is not None:
        return explicit
    prefix = f"manual.{criterion.code.lower()}.deduct."
    if not rule.rule_code.startswith(prefix):
        return {}
    match = re.fullmatch(r"(\d+)\.v1", rule.rule_code[len(prefix):])
    index = int(match[1]) - 1 if match else -1
    return entries[index] if 0 <= index < len(entries) else {}


def is_ai_entry(entry) -> bool:
    """结构化扣分条目是否来自 AI：显式的 ai_origin，或 AI 来源标记。"""

    if not isinstance(entry, dict):
        return False
    return bool(entry.get("ai_origin")) or str(entry.get("source") or "") in AI_RULE_SOURCES


def entry_ai_model(entry) -> str | None:
    """条目记录的生成模型名：采用 AI 结果时写入的 ai_model，旧条目退回起草元数据。"""

    if not isinstance(entry, dict):
        return None
    model = entry.get("ai_model") or (entry.get("generation_metadata") or {}).get("model_name")
    model = str(model).strip() if model else ""
    return model[:200] or None


def is_ai_rule(rule, criterion=None):
    """0036 起读规则上的 ``ai_origin`` 字段（存量由迁移按旧推断回填一次）。"""

    return bool(getattr(rule, "ai_origin", False)) or rule.creation_method == "llm"
