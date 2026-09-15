"""Prepare a successor without flattening imported atomic rules into criteria."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from backend.app.db import models
from backend.app.services.rubric_import.deduction_caps import normalize_ai_group_caps
from backend.app.services.rubric_import import pipeline
from backend.app.services.rubrics import lifecycle
from backend.app.services.rubrics.review_workspace import content_token, number_text
from backend.app.services.scoring.core.policy import compile_scoring_policy


def _fields(obj, names):
    return {name: deepcopy(getattr(obj, name)) for name in names.split()}


def prepare_atomic_recompile(session, version, command, edits):
    rules = session.scalars(select(models.AtomicRule).where(
        models.AtomicRule.rubric_version_id == version.id)).all()
    by_id = {rule.id: rule for rule in rules}
    if any(not isinstance(edit, dict) for edit in edits) or len(edits) != len(rules) or {edit.get("id") for edit in edits} != set(by_id):
        raise ValueError("必须完整保留当前草稿的每条原子规则，不允许通过评分项投影删除或合并")
    values = []
    expected = {}
    for edit in edits:
        rule = by_id[edit["id"]]
        if edit.get("content_token") != content_token(rule):
            raise ValueError("条款内容已变化，请刷新后重新核对")
        expected[rule.id] = edit["content_token"]
        changes = edit.get("changes", {})
        if not isinstance(changes, dict) or set(changes) - lifecycle._EDITABLE_RULE_FIELDS - {"levels"}:
            raise ValueError("原子规则包含不可编辑字段")
        try:
            normalized = lifecycle._normalize_rule_changes(rule.rule_code,
                {key: value for key, value in changes.items() if key != "levels"})
        except lifecycle.RubricLifecycleError as exc:
            raise ValueError(str(exc)) from exc
        value = _fields(rule, "rule_code name rule_text direction effect_type max_points repeat_policy cap_points judge_type checker_key checker_params evidence_policy positive_example negative_example boundary_example strictness applies_to mutex_group depends_on_rule_codes creation_method")
        value.update(normalized)
        for key in ("max_points", "cap_points"):
            value[key] = number_text(value[key])
        value["criterion_code"] = rule.criterion.code
        value["source_rule_codes"] = [source.id for source in rule.source_rules]
        levels = changes.get("levels", [
            {"code": level.level_code, **_fields(level, "points descriptor positive_example negative_example display_order")}
            for level in rule.levels])
        if not isinstance(levels, list):
            raise ValueError("分档必须为列表")
        value["levels"] = []
        seen = set()
        for index, level in enumerate(levels):
            if not isinstance(level, dict):
                raise ValueError("分档必须为对象")
            code = level.get("code")
            if not isinstance(code, str) or not code.strip() or code in seen:
                raise ValueError("分档编号必须非空且唯一")
            seen.add(code)
            try:
                points = Decimal(str(level.get("points")))
            except InvalidOperation as exc:
                raise ValueError("分档分值必须为数值") from exc
            if not points.is_finite() or points < 0 or not isinstance(level.get("descriptor"), str) or not level["descriptor"].strip():
                raise ValueError("分档需要非负有限分值和说明")
            value["levels"].append({"level_code": code, "points": number_text(points),
                "descriptor": level["descriptor"], "positive_example": level.get("positive_example"),
                "negative_example": level.get("negative_example"), "display_order": index})
        if value["direction"] == "band" and not value["levels"]:
            raise ValueError("分档规则至少需要一个档位")
        values.append(value)

    artifacts = session.scalars(select(models.SourceArtifact).where(
        models.SourceArtifact.compilation_id == version.compilation_id)).all()
    source_values, template_values, artifact_values = [], [], []
    for artifact in artifacts:
        artifact_values.append({"token": artifact.id, **_fields(artifact,
            "artifact_type file_name file_hash file_size_bytes")})
        for source in artifact.source_rules:
            source_values.append({"source_rule_code": source.source_rule_code, "source_token": source.id, "artifact_token": artifact.id,
                **_fields(source, "sheet_name row_number cell_locator raw_text")})
        for item in artifact.template_items:
            template_values.append({"item_code": item.item_code, "item_token": item.id, "artifact_token": artifact.id,
                **_fields(item, "kind section_path raw_text normalized_constraint strictness source_locator source_hash"),
                "parse_confidence": number_text(item.parse_confidence)})
    links = [{"rule_code": rule.rule_code, "template_item_code": link.template_item_id,
              **_fields(link, "relationship_type match_method rationale"),
              "match_confidence": number_text(link.match_confidence), "review_status": "pending"}
             for rule in rules for link in rule.template_links]
    rubric = deepcopy(command["rubric"])
    for criterion in rubric["criteria"]:
        criterion["deduction_rules_structured"] = normalize_ai_group_caps(
            criterion.get("deduction_rules_structured") or [],
            criterion_code=criterion["code"], maximum=criterion["max_score"])
    policy = deepcopy(rubric["global_policy"])
    policy["aggregation"]["total_score"] = number_text(rubric["total_score"])
    policy.pop("policy_hash", None)
    rubric["global_policy"] = compile_scoring_policy(policy, total_score=rubric["total_score"]).to_mapping()
    prepared = pipeline._base_graph(command=command, source_kind="atomic_edit",
        rubric=rubric, artifacts=artifact_values, source_rules=source_values,
        template_items=template_values, criteria=rubric["criteria"], atomic_rules=values,
        template_links=links, blockers=[])
    graph = prepared.to_mapping()
    graph["expected_rule_tokens"] = expected
    return pipeline.PreparedRubricGraph.from_mapping(graph)


def prepare_atomic_ai_append(session, version, command):
    """Append explicitly fingerprinted suggestions without replacing imported rules."""
    rules = session.scalars(select(models.AtomicRule).where(models.AtomicRule.rubric_version_id == version.id)).all()
    edits = [{"id": rule.id, "content_token": content_token(rule), "changes": {}} for rule in rules]
    graph = prepare_atomic_recompile(session, version, command, edits).to_mapping()
    generated = pipeline.prepare_manual_json_recompile(command=command).to_mapping()
    codes = {rule.rule_code for rule in rules}
    eligible = set()
    for criterion in command["rubric"]["criteria"]:
        for index, row in enumerate(criterion.get("deduction_rules_structured") or [], 1):
            if row.get("generation_fingerprint") and row.get("draft_row_key"):
                eligible.add(row.get("rule_code") or f"manual.{criterion['code'].lower()}.deduct.{index}.v1")
    additions = [rule for rule in generated["atomic_rules"] if rule["rule_code"] in eligible - codes]
    if not additions:
        raise ValueError("导入原子规则必须使用完整原子规则编辑，不能由评分项投影重新编译")
    graph["atomic_rules"].extend(additions)
    graph["artifacts"].extend(generated["artifacts"])
    graph["source_rules"].extend(generated["source_rules"])
    return pipeline.PreparedRubricGraph.from_mapping(graph)
