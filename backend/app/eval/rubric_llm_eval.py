"""评分规则解析中 LLM 能力的离线评估（解析重构方案阶段 5、6.5 验收）。

用人为注入已知错误的合成测试集衡量召回率与精确率：
- 规则审查：删去拆分出的规则、改分值、“各扣/每处”改成一次性、放宽 match；
- 兜底分类器：带金标签的原文单元。
需要真实模型（Mock 不能评估）；由 ``scripts/run_rubric_llm_eval.py`` 离线运行，不进入默认 pytest。
"""

from __future__ import annotations

from backend.app.services.rubric_import.classification.llm_classifier import classify_units
from backend.app.services.rubric_import.review import build_bundles
from backend.app.services.rubric_import.review import review_rules


def _criterion(code, text, rule, max_score=10):
    return {"code": code, "name": code, "max_score": max_score, "deduction_rules": [text],
            "deduction_rules_structured": [{**rule, "source_refs": [f"/criteria/{code}/deduction_rules/0"]}]}


def default_review_cases() -> list[dict]:
    return [
        {"injection": "drop_split_rule",
         "criteria": [_criterion("C01", "格式错误、图表不清、引用不规范各扣2分", {"match": "格式错误", "points": 2})],
         "expected": {"criterion_code": "C01", "types": {"granularity", "omission"}}},
        {"injection": "change_points",
         "criteria": [_criterion("C02", "文献少于20篇扣2分", {"match": "文献少于20篇", "points": 4})],
         "expected": {"criterion_code": "C02", "types": {"distortion"}}},
        {"injection": "each_to_total",
         "criteria": [_criterion("C03", "每处错别字扣0.5分，最多扣5分", {"match": "错别字", "points": 5, "repeat_policy": "once"})],
         "expected": {"criterion_code": "C03", "types": {"distortion"}}},
        {"injection": "broaden_match",
         "criteria": [_criterion("C04", "未说明数据来源扣3分", {"match": "数据", "points": 3})],
         "expected": {"criterion_code": "C04", "types": {"match_too_broad"}}},
    ]


def default_classifier_cases() -> list[dict]:
    return [
        {"unit_id": "u1", "text": "全文错别字每处扣0.5分，最多扣5分。", "label": "rule"},
        {"unit_id": "u2", "text": "查重率超过30%的论文该项不得分。", "label": "rule"},
        {"unit_id": "u3", "text": "正文不少于8000字。", "label": "requirement"},
        {"unit_id": "u4", "text": "图表须有编号和题注。", "label": "requirement"},
        {"unit_id": "u5", "text": "这里的格式再调整一下。", "label": "context"},
        {"unit_id": "u6", "text": "本表由教务处统一印制。", "label": "context"},
        {"unit_id": "u7", "text": "第 3 页", "label": "noise"},
    ]


def _require_real(scorer) -> None:
    if scorer is None or str(getattr(scorer, "provider", "")).lower() == "mock":
        raise ValueError("离线评估需要真实 AI 连接，Mock 无法衡量模型能力。")


def evaluate_review(scorer, cases=None) -> dict:
    _require_real(scorer)
    cases = cases or default_review_cases()
    hits, matching, total_findings = 0, 0, 0
    by_injection = {}
    for case in cases:
        result = review_rules(build_bundles(case["criteria"], scope="all"), scorer)
        expected = case["expected"]
        relevant = [f for f in result["findings"]
                    if f["criterion_code"] == expected["criterion_code"] and f["type"] in expected["types"]]
        total_findings += len(result["findings"])
        matching += len(relevant)
        hit = bool(relevant)
        hits += hit
        by_injection[case["injection"]] = {"hit": hit, "findings": len(result["findings"]),
                                           "discarded": len(result["discarded"])}
    return {
        "cases": len(cases),
        "recall": hits / len(cases) if cases else None,
        "precision": matching / total_findings if total_findings else None,
        "by_injection": by_injection,
    }


def evaluate_classifier(scorer, cases=None, criteria=None) -> dict:
    _require_real(scorer)
    cases = cases or default_classifier_cases()
    gold = {case["unit_id"]: case["label"] for case in cases}
    units = [{"unit_id": case["unit_id"], "text": case["text"], "context": {}} for case in cases]
    result = classify_units(units, criteria or [], scorer)
    predicted = {item["unit_id"]: item["label"] for item in result["results"]}
    correct = sum(1 for unit_id, label in gold.items() if predicted.get(unit_id) == label)
    predicted_rules = {u for u, label in predicted.items() if label == "rule"}
    gold_rules = {u for u, label in gold.items() if label == "rule"}
    true_rules = predicted_rules & gold_rules
    return {
        "cases": len(cases),
        "accuracy": correct / len(cases) if cases else None,
        "rule_precision": len(true_rules) / len(predicted_rules) if predicted_rules else None,
        "rule_recall": len(true_rules) / len(gold_rules) if gold_rules else None,
        "unclassified": len(gold) - len(predicted),
    }
