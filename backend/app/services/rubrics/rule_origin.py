"""Recover display-only provenance from the manual compiler's explicit row identity."""
import re


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


def is_ai_rule(rule, criterion):
    origin = rule_origin(rule, criterion)
    return origin.get("source") in {"ai_inferred", "llm"} or rule.creation_method == "llm"
