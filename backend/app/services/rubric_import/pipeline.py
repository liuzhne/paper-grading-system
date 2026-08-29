"""Two-phase rubric import and provenance graph persistence.

Parsing/compilation deliberately has no database dependency.  The resulting
``PreparedRubricGraph`` is immutable and can therefore be validated, queued or
retried before the short persistence transaction begins.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import re
from types import MappingProxyType
from typing import Any, Iterator
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.services.rubric_import.docx_comments import parse_comments
from backend.app.services.rubric_import.parser import _criterion_from_row
from backend.app.services.rubric_import.parser import _find_header
from backend.app.services.rubric_import.parser import _rows_with_merged_values
from backend.app.services.rubric_import.parser import parse_word_template
from backend.app.services.scoring.core.policy import build_corrected_thesis_policy
from backend.app.services.scoring.core.policy import validate_weight_configuration


IMPORT_SCHEMA_VERSION = "rubric-import-command@2"
PREPARED_SCHEMA_VERSION = "prepared-rubric-graph@1"
_DRAFT_RECOMPILE_MODE = "supersede_unpublished"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _digest_value(value: object) -> str:
    return _digest_bytes(_json_bytes(value))


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class PreparedRubricGraph(Mapping[str, object]):
    """Closed immutable transport returned by every preparation entrypoint."""

    _value: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreparedRubricGraph":
        if value.get("schema_version") != PREPARED_SCHEMA_VERSION:
            raise ValueError("unsupported prepared rubric graph schema")
        return cls(_freeze(value))  # type: ignore[arg-type]

    def __getitem__(self, key: str) -> object:
        return self._value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def to_mapping(self) -> dict[str, object]:
        return _thaw(self._value)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PersistedImportIdentity(Mapping[str, str]):
    rubric_id: str
    compilation_id: str
    rubric_version_id: str

    def __getitem__(self, key: str) -> str:
        if key not in {"rubric_id", "compilation_id", "rubric_version_id"}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("rubric_id", "compilation_id", "rubric_version_id"))

    def __len__(self) -> int:
        return 3

    def to_mapping(self) -> dict[str, str]:
        return {key: self[key] for key in self}


def _require_command(command: Mapping[str, object], source_kind: str) -> dict:
    value = deepcopy(dict(command))
    if value.get("schema_version") != IMPORT_SCHEMA_VERSION:
        raise ValueError("unsupported rubric import command schema")
    if value.get("source_kind") != source_kind:
        raise ValueError(f"expected source_kind={source_kind!r}")
    if not isinstance(value.get("rubric"), dict):
        raise ValueError("rubric import command must contain rubric metadata")
    if not isinstance(value.get("compiler"), dict):
        raise ValueError("rubric import command must contain compiler identity")
    return value


def _draft_recompile(value: object, *, required: bool = False) -> dict | None:
    """Validate the explicit draft-recovery instruction carried by a command.

    Recompilation is deliberately opt-in: silently adding a second active
    version to one draft would make rule-code based review operations
    ambiguous.  The instruction is retained in the prepared graph so the
    persistence transaction can lock and supersede the exact predecessor.
    """

    if value is None:
        if required:
            raise ValueError("draft recompile instruction is required")
        return None
    if not isinstance(value, Mapping):
        raise ValueError("draft_recompile must be an object")
    directive = deepcopy(dict(value))
    if set(directive) != {"mode", "supersedes_compilation_id"}:
        raise ValueError(
            "draft_recompile must contain only mode and supersedes_compilation_id"
        )
    if directive.get("mode") != _DRAFT_RECOMPILE_MODE:
        raise ValueError("unsupported draft recompile mode")
    predecessor_id = directive.get("supersedes_compilation_id")
    if not isinstance(predecessor_id, str) or not predecessor_id.strip():
        raise ValueError("supersedes_compilation_id must be a non-empty string")
    directive["supersedes_compilation_id"] = predecessor_id.strip()
    return directive


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: object, *, default: str = "0") -> Decimal:
    if value is None or _text(value) == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _number_text(value: object) -> str:
    number = _decimal(value)
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _json_object(value: object) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def _split(value: object) -> list[str]:
    text = _text(value)
    return [part.strip() for part in re.split(r"[；;、\n]+", text) if part.strip()]


def _parse_levels(value: object) -> list[dict]:
    levels = []
    for order, part in enumerate(_split(value)):
        if ":" not in part:
            continue
        code, points = part.split(":", 1)
        levels.append(
            {
                "level_code": code.strip(),
                "points": _number_text(points),
                "descriptor": code.strip(),
                "positive_example": None,
                "negative_example": None,
                "display_order": order,
            }
        )
    return levels


def _parse_deduction_points(value: object) -> str | None:
    numbers = re.findall(r"\d+(?:\.\d+)?", _text(value))
    return _number_text(numbers[-1]) if numbers else None


def _slug(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()
    return normalized or fallback


def _criterion_projection(criterion: Mapping[str, object]) -> dict:
    levels = []
    for item in criterion.get("rubric_levels") or criterion.get("levels") or []:
        level = deepcopy(dict(item))
        level_code = _text(
            level.get("level_code") or level.get("code") or level.get("label")
        )
        descriptor = _text(level.get("descriptor") or level.get("label"))
        # Legacy score-input adapters key bands by ``label``; the provenance
        # graph keys them by ``level_code``.  The compatibility projection must
        # state both instead of forcing consumers to guess.
        level["level_code"] = level_code
        level["label"] = _text(level.get("label")) or descriptor or level_code
        levels.append(level)
    return {
        "code": criterion["code"],
        "name": criterion["name"],
        "max_score": criterion["max_score"],
        "weight": criterion.get("weight"),
        "description": criterion.get("description"),
        "evidence_hints": list(criterion.get("evidence_hints") or []),
        "deduction_rules": list(criterion.get("deduction_rules") or []),
        "display_order": criterion.get("display_order", 0),
        "criterion_type": criterion.get("criterion_type") or "llm_judgment",
        "scoring_mode": criterion.get("scoring_mode") or "review_only",
        "applies_to": criterion.get("applies_to") or "global",
        "rubric_levels": levels,
        "sub_checks": deepcopy(criterion.get("sub_checks") or []),
        "dimension": criterion.get("dimension"),
        "deduction_rules_structured": deepcopy(
            criterion.get("deduction_rules_structured") or []
        ),
    }


def _rule(
    *,
    rule_code: str,
    criterion_code: str,
    name: str,
    rule_text: str,
    direction: str,
    effect_type: str,
    judge_type: str,
    checker_key: str | None,
    checker_params: Mapping[str, object] | None,
    evidence_policy: Mapping[str, object] | None,
    max_points: object = None,
    repeat_policy: str | None = None,
    cap_points: object = None,
    levels: list[dict] | None = None,
    strictness: str = "required",
    applies_to: str = "global",
    creation_method: str = "compiler",
    source_rule_codes: list[str] | None = None,
) -> dict:
    normalized_evidence = deepcopy(dict(evidence_policy or {}))
    if not normalized_evidence:
        normalized_evidence = {
            "mode": (
                "deterministic_observation"
                if judge_type == "deterministic"
                else "source_quote"
            ),
            "requirement": "required",
            "minimum_coverage": "1",
        }
    return {
        "rule_code": rule_code,
        "criterion_code": criterion_code,
        "name": name,
        "rule_text": rule_text or name,
        "direction": direction,
        "effect_type": effect_type,
        "max_points": None if max_points is None else _number_text(max_points),
        "repeat_policy": repeat_policy,
        "cap_points": None if cap_points is None else _number_text(cap_points),
        "judge_type": judge_type,
        "checker_key": checker_key,
        "checker_params": deepcopy(dict(checker_params or {})),
        "evidence_policy": normalized_evidence,
        "positive_example": None,
        "negative_example": None,
        "boundary_example": None,
        "strictness": strictness if strictness in {"required", "preferred", "unknown"} else "required",
        "applies_to": applies_to or "global",
        "mutex_group": None,
        "depends_on_rule_codes": [],
        "status": "draft",
        "creation_method": creation_method,
        "reviewed_by": None,
        "reviewed_at": None,
        "levels": deepcopy(levels or []),
        "source_rule_codes": list(source_rule_codes or []),
    }


def _base_graph(
    *,
    command: dict,
    source_kind: str,
    rubric: dict,
    artifacts: list[dict],
    source_rules: list[dict],
    template_items: list[dict],
    criteria: list[dict],
    atomic_rules: list[dict],
    template_links: list[dict],
    blockers: list[dict],
    warnings: list[object] | None = None,
    raw_parse_output: dict | None = None,
    raw_model_output: dict | None = None,
) -> PreparedRubricGraph:
    compiler = command["compiler"]
    version_input = command.get("version") or {}
    global_policy = deepcopy(rubric.get("global_policy") or {})
    if not global_policy:
        weight_mode = validate_weight_configuration(
            criteria,
            total_score=rubric["total_score"],
        ).mode
        global_policy = build_corrected_thesis_policy(
            rubric["total_score"],
            weight_mode,
        ).to_mapping()
    validation = {"valid": not blockers}
    compilation = {
        "status": "validated" if not blockers else "blocked",
        "parser_version": compiler["parser_version"],
        "compiler_version": compiler["compiler_version"],
        "prompt_version": compiler["prompt_version"],
        "model_provider": compiler.get("model_provider"),
        "model_name": compiler.get("model_name"),
        "sampling_params": deepcopy(compiler.get("sampling_params") or {}),
        "raw_parse_output": deepcopy(
            raw_parse_output
            or {
                "source_kind": source_kind,
                "criteria": deepcopy(criteria),
            }
        ),
        "raw_model_output": deepcopy(raw_model_output or {}),
        "validation_result": validation,
        "blockers": deepcopy(blockers),
        "warnings": deepcopy(warnings or []),
        "human_changes": [],
    }
    projections = [_criterion_projection(item) for item in criteria]
    graph = {
        "schema_version": PREPARED_SCHEMA_VERSION,
        "source_kind": source_kind,
        "rubric": deepcopy(rubric),
        "compilation": compilation,
        "artifacts": deepcopy(artifacts),
        "source_rules": deepcopy(source_rules),
        "template_items": deepcopy(template_items),
        "criteria": deepcopy(criteria),
        "atomic_rules": deepcopy(atomic_rules),
        "template_links": deepcopy(template_links),
        "legacy_projection": {"criteria": projections},
        "draft_recompile": _draft_recompile(command.get("draft_recompile")),
        "version": {
            "version": version_input.get("version") or rubric.get("version") or "1.0.0",
            "workflow_profile": rubric.get("workflow_profile") or "template_driven",
            "global_policy": global_policy,
            "business_profile_key": rubric.get("business_profile_key") or "thesis",
            "hash_scheme": version_input.get("hash_scheme") or "rubric-content-v2",
        },
    }
    return PreparedRubricGraph.from_mapping(graph)


def _excel_rows(rules_bytes: bytes) -> tuple[str, list[dict], list[str]]:
    workbook = load_workbook(BytesIO(rules_bytes), data_only=True)
    warnings: list[str] = []
    for sheet in workbook.worksheets:
        rows = _rows_with_merged_values(sheet)
        header_index, mapping = _find_header(rows)
        if header_index is not None:
            headers = [_text(value) for value in rows[header_index]]
            records = []
            for row_number, values in enumerate(
                rows[header_index + 1 :], start=header_index + 2
            ):
                criterion = _criterion_from_row(values, mapping, len(records) + 1)
                if criterion is None:
                    continue
                # Keep every original cell for provenance, then add the
                # canonical fields consumed by the auditable compiler.
                record = {
                    header: values[index] if index < len(values) else None
                    for index, header in enumerate(headers)
                    if header
                }
                record.update(
                    {
                        "编号": criterion.code,
                        "评分项": criterion.name,
                        "分值": criterion.max_score,
                        "评分说明": criterion.description,
                    }
                )
                record["__row_number__"] = row_number
                records.append(record)
            if records:
                return sheet.title, records, warnings
        warnings.append(f"工作表 {sheet.title} 未识别到评分规则表头，已跳过。")
    raise ValueError("Excel 未解析到有效评分项，请确认包含评分项名称和分值列。")


def _make_source_rule(record: dict, *, sheet_name: str, fallback_code: str) -> dict:
    row_number = int(record["__row_number__"])
    source_code = _text(record.get("原子规则编号") or record.get("编号")) or fallback_code
    raw = {
        key: value
        for key, value in record.items()
        if not key.startswith("__") and value not in (None, "")
    }
    return {
        "source_rule_code": source_code,
        "sheet_name": sheet_name,
        "row_number": row_number,
        "cell_locator": f"A{row_number}:{chr(64 + min(max(len(raw), 1), 26))}{row_number}",
        "raw_text": json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str),
        "artifact_token": "excel",
    }


def _hybrid_nodes(record: dict, source_code: str, order: int, policy: Mapping[str, object]):
    entries = []
    for line in _text(record.get("子检查")).splitlines():
        parts = [item.strip() for item in line.split("|")]
        if len(parts) != 3:
            continue
        entries.append((parts[0], parts[1].lower(), _decimal(parts[2])))
    total = sum((points for _, _, points in entries), Decimal("0"))
    parent_weight = None if record.get("权重") in (None, "") else _decimal(record["权重"])
    weighted = (policy.get("aggregation") or {}).get("mode") == "weighted_normalized"
    exact = True
    weights: list[str | None] = []
    for _, _, points in entries:
        if weighted and parent_weight is not None and total:
            calculated = parent_weight * points / total
            if calculated != calculated.quantize(Decimal("0.01")):
                exact = False
                weights.append(None)
            else:
                weights.append(_number_text(calculated))
        else:
            weights.append(None)

    criteria = []
    rules = []
    for index, ((name, judge, points), weight) in enumerate(zip(entries, weights)):
        suffix = _slug(name, f"LEAF_{index + 1}")
        code = f"{source_code}.{suffix}"[:50]
        scoring_mode = "deductive" if judge == "deterministic" else "banded"
        levels = []
        if scoring_mode == "banded":
            levels = [
                {
                    "level_code": "HIGH",
                    "points": _number_text(points),
                    "descriptor": "要求完整满足。",
                    "positive_example": None,
                    "negative_example": None,
                    "display_order": 0,
                },
                {
                    "level_code": "LOW",
                    "points": _number_text(points / Decimal("2")),
                    "descriptor": "要求部分满足。",
                    "positive_example": None,
                    "negative_example": None,
                    "display_order": 1,
                },
            ]
        criterion = {
            "code": code,
            "name": name,
            "max_score": _number_text(points),
            "weight": weight,
            "description": name,
            "evidence_hints": [],
            "deduction_rules": [],
            "display_order": order + index,
            "criterion_type": judge,
            "scoring_mode": scoring_mode,
            "applies_to": "global",
            "rubric_levels": deepcopy(levels),
            "sub_checks": [],
            "dimension": None,
            "deduction_rules_structured": [],
            "parent_criterion_code": source_code,
        }
        rule_code = f"{source_code.lower()}.{suffix.lower()}.v1"
        rules.append(
            _rule(
                rule_code=rule_code,
                criterion_code=code,
                name=name,
                rule_text=name,
                direction="deduct" if scoring_mode == "deductive" else "band",
                effect_type="score",
                judge_type=judge,
                checker_key=("generic.hybrid.required.v1" if judge == "deterministic" else None),
                checker_params={},
                evidence_policy={
                    "mode": "deterministic_observation" if judge == "deterministic" else "source_quote",
                    "requirement": "required",
                    "minimum_coverage": "1",
                },
                max_points=points if scoring_mode == "deductive" else None,
                repeat_policy="once" if scoring_mode == "deductive" else None,
                levels=levels,
                source_rule_codes=[source_code],
            )
        )
        criteria.append(criterion)
    return criteria, rules, exact


def _template_matches(criterion: Mapping[str, object], template_hints: list[str]) -> list[str]:
    """Return the established Word-template hints for one criterion."""

    text = " ".join(
        (
            _text(criterion.get("name")),
            _text(criterion.get("description")),
            " ".join(str(item) for item in criterion.get("evidence_hints") or []),
        )
    )
    matched = [hint for hint in template_hints if hint in text]
    semantic_groups = (
        ("文献", ("文献综述", "国内外研究现状", "相关工作")),
        ("方法", ("研究方法", "实验设计", "数据来源")),
        ("创新", ("创新点",)),
        ("写作", ("中文摘要", "英文摘要", "关键词", "目录", "结论")),
        ("规范", ("中文摘要", "英文摘要", "关键词", "目录", "结论")),
        ("参考文献", ("参考文献",)),
        ("选题", ("绪论", "研究背景", "研究意义")),
        ("意义", ("绪论", "研究背景", "研究意义")),
        ("论证", ("结果分析", "讨论", "结论")),
        ("分析", ("结果分析", "讨论")),
    )
    for keyword, candidates in semantic_groups:
        if keyword not in text:
            continue
        for hint in candidates:
            if hint in template_hints and hint not in matched:
                matched.append(hint)
    return matched[:8]


def _enrich_projection_from_template(
    criteria: list[dict], template_summary: Mapping[str, object]
) -> None:
    hints = [str(item) for item in template_summary.get("hints") or []]
    for criterion in criteria:
        matched = _template_matches(criterion, hints)
        if not matched:
            continue
        evidence_hints = list(criterion.get("evidence_hints") or [])
        for hint in matched:
            if hint not in evidence_hints:
                evidence_hints.append(hint)
        criterion["evidence_hints"] = evidence_hints
        note = "Word 模板解析提示：建议重点查看 %s。" % "、".join(matched[:6])
        description = _text(criterion.get("description"))
        criterion["description"] = f"{description}\n{note}" if description else note


def prepare_file_import(
    *,
    command: Mapping[str, object],
    rules_bytes: bytes,
    template_bytes: bytes | None = None,
    scorer=None,
) -> PreparedRubricGraph:
    value = _require_command(command, "file_import")
    if not isinstance(rules_bytes, (bytes, bytearray)) or not rules_bytes:
        raise ValueError("rules_bytes must contain an Excel workbook")
    rules_data = bytes(rules_bytes)
    template_data = bytes(template_bytes) if template_bytes is not None else None
    sheet_name, records, warnings = _excel_rows(rules_data)
    rubric_input = deepcopy(value["rubric"])
    files = value.get("files") or {}
    artifacts = [
        {
            "artifact_type": "excel",
            "file_name": files.get("rules_file_name") or "rules.xlsx",
            "file_hash": _digest_bytes(rules_data),
            "file_size_bytes": len(rules_data),
            "token": "excel",
        }
    ]
    template_items: list[dict] = []
    template_summary: dict = {}
    if template_data is not None:
        artifacts.append(
            {
                "artifact_type": "word",
                "file_name": files.get("template_file_name") or "template.docx",
                "file_hash": _digest_bytes(template_data),
                "file_size_bytes": len(template_data),
                "token": "word",
            }
        )
        for index, comment in enumerate(parse_comments(template_data)):
            section = _text(comment.get("section_title"))
            raw_text = _text(comment.get("comment_text"))
            template_items.append(
                {
                    "item_code": f"WORD-COMMENT-{comment.get('comment_id') or index + 1}",
                    "kind": "comment",
                    "section_path": [section] if section else [],
                    "raw_text": raw_text,
                    "normalized_constraint": {"text": raw_text},
                    "strictness": "required",
                    "source_locator": {
                        "comment_id": str(comment.get("comment_id") or index + 1),
                        "anchor_text": _text(comment.get("anchor_text")),
                        "section_title": section,
                    },
                    "source_hash": _digest_bytes(raw_text.encode("utf-8")),
                    "parse_confidence": "1",
                    "artifact_token": "word",
                }
            )
        try:
            template_summary = parse_word_template(template_data)
            rubric_input["format_spec"] = deepcopy(template_summary.get("format_spec") or {})
        except Exception as exc:  # format extraction is non-authoritative provenance
            warnings.append({"code": "TEMPLATE_FORMAT_PARSE_WARNING", "message": str(exc)})

    source_rules: list[dict] = []
    criteria: list[dict] = []
    atomic_rules: list[dict] = []
    template_links: list[dict] = []
    blockers: list[dict] = []
    raw_model_output: dict = {}
    for order, record in enumerate(records):
        fallback = f"SOURCE-{order + 1:03d}"
        source = _make_source_rule(record, sheet_name=sheet_name, fallback_code=fallback)
        source_rules.append(source)
        source_code = source["source_rule_code"]
        name = _text(record.get("评分项"))
        max_score = _number_text(record.get("分值"))
        criterion_code = _text(record.get("评分项编号")) or _slug(source_code, fallback)
        kind = _text(record.get("类型")).lower() or "llm_judgment"
        explicit_mode = _text(record.get("评分模式")).lower()
        levels = _parse_levels(record.get("分档"))
        if kind == "hybrid" or explicit_mode == "hybrid" or _text(record.get("子检查")):
            leaf_criteria, leaf_rules, exact = _hybrid_nodes(
                record,
                source_code,
                order,
                rubric_input.get("global_policy") or {},
            )
            criteria.extend(leaf_criteria)
            atomic_rules.extend(leaf_rules)
            if not exact:
                blockers.append(
                    {
                        "code": "HYBRID_WEIGHT_NOT_EXACT",
                        "criterion_code": source_code,
                        "message": "混合评分项的子项权重无法精确表示",
                    }
                )
            continue

        deduction_text = _text(record.get("扣分规则"))
        deduction_points = _parse_deduction_points(deduction_text)
        description_text = _text(record.get("评分说明"))
        checker_key = _text(record.get("checker_key")) or None
        if (
            _decimal(max_score) > 0
            and not description_text
            and not deduction_text
            and not levels
            and checker_key is None
        ):
            blockers.append(
                {
                    "code": "MISSING_CRITERION_DESCRIPTION",
                    "criterion_code": criterion_code,
                    "message": (
                        "分值大于零的评分项必须配置评分说明、分档、扣分规则或确定性检查器"
                    ),
                }
            )
        scoring_mode = explicit_mode or (
            "banded" if levels else "deductive" if deduction_points else "review_only"
        )
        if scoring_mode in {"deduct", "deductive"}:
            scoring_mode = "deductive"
        elif scoring_mode in {"band", "banded"}:
            scoring_mode = "banded"
        elif scoring_mode in {"review", "review_only"}:
            scoring_mode = "review_only"
        applies_to = _text(record.get("适用范围")) or "global"
        evidence_hints = _split(record.get("证据提示"))
        criterion = {
            "code": criterion_code,
            "name": name,
            "max_score": max_score,
            "weight": None if record.get("权重") in (None, "") else _number_text(record.get("权重")),
            "description": description_text or None,
            "evidence_hints": evidence_hints,
            "deduction_rules": [deduction_text] if deduction_text else [],
            "display_order": order,
            "criterion_type": "deterministic" if kind == "deterministic" else "llm_judgment",
            "scoring_mode": scoring_mode,
            "applies_to": applies_to,
            "rubric_levels": deepcopy(levels),
            "sub_checks": [],
            "dimension": _text(record.get("维度")) or None,
            "deduction_rules_structured": [],
        }
        criteria.append(criterion)
        checker_params = _json_object(record.get("checker_params"))
        evidence_policy = _json_object(record.get("evidence_policy"))
        strictness = _text(record.get("strictness")) or "required"
        effect_type = _text(record.get("effect_type")) or ("review" if scoring_mode == "review_only" else "score")
        repeat_policy = _text(record.get("repeat_policy")) or None
        creation_method = "compiler"
        if scoring_mode == "deductive" and deduction_points:
            direction = "deduct"
            rule_max = deduction_points
            repeat_policy = repeat_policy or "once"
            criterion["deduction_rules_structured"] = [
                {
                    "match": re.sub(r"扣\s*\d+(?:\.\d+)?\s*分?", "", deduction_text).strip(),
                    "points": _number_text(deduction_points),
                    "reason": deduction_text,
                    "source": "excel",
                }
            ]
        elif scoring_mode == "banded" and levels:
            direction = "band"
            rule_max = None
            repeat_policy = None
        else:
            scoring_mode = criterion["scoring_mode"] = "review_only"
            direction = "none"
            effect_type = "review"
            rule_max = None
            repeat_policy = None
            if explicit_mode not in {"review", "review_only"}:
                blockers.append(
                    {
                        "code": "MISSING_EXECUTABLE_SCORING_MODE",
                        "criterion_code": criterion_code,
                        "message": "评分项必须明确配置分档评分、扣分评分或仅人工复核",
                    }
                )
            if scorer is not None and deduction_text:
                result = scorer.complete_json(
                    "Compile an auditable draft rule; never authorize publication.",
                    {
                        "criterion": deepcopy(criterion),
                        "deduction_rules_text": [deduction_text],
                    },
                )
                raw_model_output = deepcopy(result or {})
                creation_method = "llm"
        rule_code = source_code if "." in source_code else f"{source_code.lower()}.v1"
        atomic_rules.append(
            _rule(
                rule_code=rule_code,
                criterion_code=criterion_code,
                name=name,
                rule_text=_text(record.get("评分说明")) or deduction_text or name,
                direction=direction,
                effect_type=effect_type,
                judge_type="deterministic" if kind == "deterministic" else "semantic",
                checker_key=checker_key,
                checker_params=checker_params,
                evidence_policy=evidence_policy,
                max_points=rule_max,
                repeat_policy=repeat_policy,
                levels=levels,
                strictness=strictness,
                applies_to=applies_to,
                creation_method=creation_method,
                source_rule_codes=[source_code],
            )
        )

    if template_summary:
        # The legacy projection is generated before persistence, together with
        # the authoritative graph.  This keeps Word enrichment inside the same
        # short transaction instead of patching criteria after commit.
        _enrich_projection_from_template(criteria, template_summary)

    for rule in atomic_rules:
        for item in template_items:
            section = " ".join(item.get("section_path") or [])
            anchor = _text(item.get("source_locator", {}).get("anchor_text"))
            applies_to = _text(rule.get("applies_to"))
            if applies_to != "global" and (
                applies_to in section or section in applies_to or applies_to in anchor
            ):
                template_links.append(
                    {
                        "rule_code": rule["rule_code"],
                        "template_item_code": item["item_code"],
                        "relationship_type": "constraint",
                        "match_method": "heuristic",
                        "match_confidence": "1",
                        "rationale": "Word comment applies to the rule scope.",
                        "review_status": "pending",
                    }
                )

    rubric = {
        "name": rubric_input.get("name") or "Imported rubric",
        "version": rubric_input.get("version") or "1.0.0",
        "description": rubric_input.get("description"),
        "total_score": _number_text(
            (rubric_input.get("global_policy") or {}).get("aggregation", {}).get("total_score")
            or sum((_decimal(item["max_score"]) for item in criteria), Decimal("0"))
        ),
        "format_spec": deepcopy(rubric_input.get("format_spec") or {}),
        "business_profile_key": rubric_input.get("business_profile_key") or "thesis",
        "workflow_profile": rubric_input.get("workflow_profile") or "template_driven",
        "global_policy": deepcopy(rubric_input.get("global_policy") or {}),
    }
    return _base_graph(
        command=value,
        source_kind="file_import",
        rubric=rubric,
        artifacts=artifacts,
        source_rules=source_rules,
        template_items=template_items,
        criteria=criteria,
        atomic_rules=atomic_rules,
        template_links=template_links,
        blockers=blockers,
        warnings=warnings,
        raw_parse_output={
            "sheet_name": sheet_name,
            "criteria": deepcopy(criteria),
            "source_rules": deepcopy(source_rules),
            "template_items": deepcopy(template_items),
        },
        raw_model_output=raw_model_output,
    )


def _manual_nodes(payload: Mapping[str, object]):
    criteria = []
    rules = []
    source_rules = []
    blockers = []
    for order, raw in enumerate(payload.get("criteria") or []):
        item = deepcopy(dict(raw))
        code = _text(item.get("code")) or f"MANUAL_{order + 1}"
        levels = []
        for index, level in enumerate(item.get("levels") or item.get("rubric_levels") or []):
            level_code = _text(
                level.get("level_code")
                or level.get("code")
                or level.get("label")
            ) or f"LEVEL_{index + 1}"
            levels.append(
                {
                    "level_code": level_code,
                    "points": _number_text(level.get("points")),
                    "descriptor": (
                        _text(level.get("descriptor") or level.get("label"))
                        or level_code
                    ),
                    "positive_example": level.get("positive_example"),
                    "negative_example": level.get("negative_example"),
                    "display_order": int(level.get("display_order", index)),
                }
            )
        mode = _text(item.get("scoring_mode")) or ("banded" if levels else "review_only")
        projection = {
            "code": code,
            "name": _text(item.get("name")) or code,
            "max_score": _number_text(item.get("max_score")),
            "weight": None if item.get("weight") is None else _number_text(item.get("weight")),
            "description": item.get("description"),
            "evidence_hints": deepcopy(item.get("evidence_hints") or []),
            "deduction_rules": deepcopy(item.get("deduction_rules") or []),
            "display_order": int(item.get("display_order", order)),
            "criterion_type": item.get("criterion_type") or "llm_judgment",
            "scoring_mode": mode,
            "applies_to": item.get("applies_to") or "global",
            "rubric_levels": deepcopy(levels),
            "sub_checks": deepcopy(item.get("sub_checks") or []),
            "dimension": item.get("dimension"),
            "deduction_rules_structured": deepcopy(item.get("deduction_rules_structured") or []),
        }
        criteria.append(projection)
        source_rules.append(
            {
                "source_rule_code": code,
                "sheet_name": "manual_json",
                "row_number": order + 1,
                "cell_locator": f"/criteria/{order}",
                "raw_text": json.dumps(item, ensure_ascii=False, sort_keys=True),
                "artifact_token": "manual_json",
            }
        )
        if mode == "banded" and levels:
            rules.append(
                _rule(
                    rule_code=f"manual.{code.lower()}.band.v1",
                    criterion_code=code,
                    name=projection["name"],
                    rule_text=projection["description"] or projection["name"],
                    direction="band",
                    effect_type="score",
                    judge_type="semantic",
                    checker_key=None,
                    checker_params={},
                    evidence_policy={
                        "mode": "source_quote",
                        "requirement": "required",
                        "minimum_coverage": "1",
                    },
                    levels=levels,
                    applies_to=projection["applies_to"],
                    creation_method="manual",
                    source_rule_codes=[code],
                )
            )
        elif mode in {"deductive", "deduct"} and projection["deduction_rules_structured"]:
            compiled_count = 0
            for deduction_index, deduction in enumerate(
                projection["deduction_rules_structured"], start=1
            ):
                if not isinstance(deduction, Mapping):
                    continue
                points = _decimal(
                    deduction.get("points") or deduction.get("max_points")
                )
                if points <= 0 or points > _decimal(projection["max_score"]):
                    continue
                compiled_count += 1
                judge_type = (
                    "deterministic"
                    if projection["criterion_type"] == "deterministic"
                    else "semantic"
                )
                match = deepcopy(deduction.get("match"))
                rule_text = _text(deduction.get("reason")) or (
                    json.dumps(match, ensure_ascii=False, sort_keys=True)
                    if isinstance(match, (dict, list))
                    else _text(match)
                ) or projection["name"]
                rules.append(
                    _rule(
                        rule_code=(
                            f"manual.{code.lower()}.deduct.{deduction_index}.v1"
                        ),
                        criterion_code=code,
                        name=f"{projection['name']} #{deduction_index}",
                        rule_text=rule_text,
                        direction="deduct",
                        effect_type="score",
                        judge_type=judge_type,
                        checker_key=(
                            deduction.get("checker_key")
                            or "thesis.legacy_required_fields.v1"
                            if judge_type == "deterministic"
                            else None
                        ),
                        checker_params=(
                            deduction.get("checker_params")
                            or {"match": match}
                            if judge_type == "deterministic"
                            else {}
                        ),
                        evidence_policy=(
                            {
                                "mode": "deterministic_observation",
                                "requirement": "required",
                                "minimum_coverage": "1",
                            }
                            if judge_type == "deterministic"
                            else {
                                "mode": "source_quote",
                                "requirement": "required",
                                "minimum_coverage": "1",
                            }
                        ),
                        max_points=points,
                        repeat_policy=deduction.get("repeat_policy") or "once",
                        cap_points=deduction.get("cap_points"),
                        applies_to=projection["applies_to"],
                        creation_method="manual",
                        source_rule_codes=[code],
                    )
                )
            if compiled_count == 0:
                blockers.append(
                    {
                        "code": "MISSING_EXECUTABLE_SCORING_MODE",
                        "criterion_code": code,
                        "message": "扣分制评分项没有有效的结构化扣分规则",
                    }
                )
        elif mode == "review_only":
            rules.append(
                _rule(
                    rule_code=f"manual.{code.lower()}.review.v1",
                    criterion_code=code,
                    name=projection["name"],
                    rule_text=projection["description"] or projection["name"],
                    direction="none",
                    effect_type="review",
                    judge_type="semantic",
                    checker_key=None,
                    checker_params={},
                    evidence_policy={
                        "mode": "source_quote",
                        "requirement": "required",
                        "minimum_coverage": "1",
                    },
                    applies_to=projection["applies_to"],
                    creation_method="manual",
                    source_rule_codes=[code],
                )
            )
        else:
            blockers.append(
                {
                    "code": "MISSING_EXECUTABLE_SCORING_MODE",
                    "criterion_code": code,
                    "message": "手工评分项必须明确配置可执行的评分方式",
                }
            )
            projection["scoring_mode"] = "review_only"
            # Keep a complete, inspectable graph for blocked legacy/direct
            # inputs.  Unlike an explicitly configured review_only criterion,
            # this fallback must retain the publication blocker above.
            rules.append(
                _rule(
                    rule_code=f"manual.{code.lower()}.review.v1",
                    criterion_code=code,
                    name=projection["name"],
                    rule_text=projection["description"] or projection["name"],
                    direction="none",
                    effect_type="review",
                    judge_type="semantic",
                    checker_key=None,
                    checker_params={},
                    evidence_policy={"mode": "source_quote", "requirement": "required", "minimum_coverage": "1"},
                    applies_to=projection["applies_to"],
                    creation_method="manual",
                    source_rule_codes=[code],
                )
            )
    return criteria, rules, source_rules, blockers


def prepare_manual_json_import(*, command: Mapping[str, object]) -> PreparedRubricGraph:
    value = _require_command(command, "manual_json")
    payload = deepcopy(value["rubric"])
    data = _json_bytes(payload)
    criteria, rules, source_rules, blockers = _manual_nodes(payload)
    rubric = {
        "name": payload.get("name") or "Manual rubric",
        "version": payload.get("version") or "1.0.0",
        "description": payload.get("description"),
        "total_score": _number_text(payload.get("total_score")),
        "format_spec": deepcopy(payload.get("format_spec") or {}),
        "business_profile_key": payload.get("business_profile_key") or "thesis",
        "workflow_profile": payload.get("workflow_profile") or "manual_json",
        "global_policy": deepcopy(payload.get("global_policy") or {}),
    }
    return _base_graph(
        command=value,
        source_kind="manual_json",
        rubric=rubric,
        artifacts=[
            {
                "artifact_type": "manual_json",
                "file_name": "rubric.json",
                "file_hash": _digest_bytes(data),
                "file_size_bytes": len(data),
                "token": "manual_json",
            }
        ],
        source_rules=source_rules,
        template_items=[],
        criteria=criteria,
        atomic_rules=rules,
        template_links=[],
        blockers=blockers,
        raw_parse_output={"criteria": deepcopy(criteria), "source_kind": "manual_json"},
    )


def prepare_manual_json_recompile(
    *, command: Mapping[str, object]
) -> PreparedRubricGraph:
    """Prepare an explicit manual recovery graph for an existing draft.

    This is intentionally a narrow wrapper rather than an in-place editor.  A
    blocked compilation remains immutable/auditable and a complete new graph
    is produced from explicit band, deductive, or review-only criterion data.
    """

    value = deepcopy(dict(command))
    _draft_recompile(value.get("draft_recompile"), required=True)
    return prepare_manual_json_import(command=value)


def prepare_legacy_draft_upgrade(*, command: Mapping[str, object]) -> PreparedRubricGraph:
    value = _require_command(command, "legacy_draft_upgrade")
    legacy = deepcopy(value.get("legacy_rubric") or {})
    if legacy.get("schema_version") != "legacy-rubric-draft@1":
        raise ValueError("legacy upgrade requires legacy-rubric-draft@1")
    merged = {
        **legacy,
        **deepcopy(value["rubric"]),
        "criteria": deepcopy(legacy.get("criteria") or []),
    }
    mapping = deepcopy(value.get("upgrade_mapping") or {})
    if mapping:
        mapped_criteria = []
        for criterion in merged["criteria"]:
            code = criterion["code"]
            instruction = mapping.get(code)
            if instruction is None:
                mapped_criteria.append(criterion)
                continue
            replacement = deepcopy(criterion)
            replacement.update(
                {
                    "criterion_type": instruction.get("criterion_type") or "llm_judgment",
                    "scoring_mode": instruction.get("scoring_mode") or "banded",
                    "levels": [
                        {
                            "code": level.get("level_code"),
                            "points": level.get("points"),
                            "descriptor": level.get("descriptor"),
                            "display_order": level.get("display_order", index),
                        }
                        for index, level in enumerate(instruction.get("levels") or [])
                    ],
                }
            )
            mapped_criteria.append(replacement)
        merged["criteria"] = mapped_criteria
    criteria, rules, source_rules, blockers = _manual_nodes(merged)
    for source in source_rules:
        source["sheet_name"] = "legacy_draft"
        source["artifact_token"] = "legacy_draft"
    for rule in rules:
        rule["creation_method"] = "legacy_upgrade"
        if mapping:
            instruction = mapping.get(rule["criterion_code"])
            if instruction:
                rule["evidence_policy"] = deepcopy(instruction.get("evidence_policy") or rule["evidence_policy"])
    data = _json_bytes(legacy)
    rubric = {
        "name": legacy.get("name") or "Legacy rubric",
        "version": legacy.get("version") or "legacy-v1",
        "description": legacy.get("description"),
        "total_score": _number_text(legacy.get("total_score")),
        "format_spec": deepcopy(legacy.get("format_spec") or {}),
        "business_profile_key": merged.get("business_profile_key") or "thesis",
        "workflow_profile": merged.get("workflow_profile") or "legacy_upgrade",
        "global_policy": deepcopy(merged.get("global_policy") or {}),
    }
    return _base_graph(
        command=value,
        source_kind="legacy_draft_upgrade",
        rubric=rubric,
        artifacts=[
            {
                "artifact_type": "legacy_draft",
                "file_name": "legacy-rubric.json",
                "file_hash": _digest_bytes(data),
                "file_size_bytes": len(data),
                "token": "legacy_draft",
            }
        ],
        source_rules=source_rules,
        template_items=[],
        criteria=criteria,
        atomic_rules=rules,
        template_links=[],
        blockers=blockers,
        raw_parse_output={"criteria": deepcopy(criteria), "source_kind": "legacy_draft_upgrade"},
    )


def _apply_projection(rubric: models.Rubric, projections: list[Mapping[str, object]]) -> dict[str, models.RubricCriterion]:
    existing = {item.code: item for item in rubric.criteria}
    result: dict[str, models.RubricCriterion] = {}
    for order, value in enumerate(projections):
        code = str(value["code"])
        criterion = existing.get(code)
        if criterion is None:
            criterion = models.RubricCriterion(
                id=models.new_id(),
                rubric_id=rubric.id,
                rubric=rubric,
                code=code,
            )
        criterion.name = str(value["name"])
        criterion.max_score = _decimal(value["max_score"])
        criterion.weight = None if value.get("weight") is None else _decimal(value["weight"])
        criterion.description = value.get("description")
        criterion.evidence_hints = deepcopy(list(value.get("evidence_hints") or []))
        criterion.deduction_rules = deepcopy(list(value.get("deduction_rules") or []))
        criterion.display_order = int(value.get("display_order", order))
        criterion.criterion_type = str(value.get("criterion_type") or "llm_judgment")
        criterion.scoring_mode = str(value.get("scoring_mode") or "review_only")
        criterion.applies_to = str(value.get("applies_to") or "global")
        criterion.rubric_levels = deepcopy(list(value.get("rubric_levels") or []))
        criterion.sub_checks = deepcopy(list(value.get("sub_checks") or []))
        criterion.dimension = value.get("dimension")
        criterion.deduction_rules_structured = deepcopy(
            list(value.get("deduction_rules_structured") or [])
        )
        result[code] = criterion
    return result


def _recompile_event(
    *,
    action: str,
    before: object,
    after: object,
    actor_id: str,
    reason: str,
    occurred_at: datetime,
) -> dict:
    """Build the same immutable nine-field audit envelope as lifecycle edits."""

    return {
        "change_id": str(uuid4()),
        "rule_code": "__rubric__",
        "field_path": "/compilation/status",
        "action": action,
        "before": deepcopy(before),
        "after": deepcopy(after),
        "actor_id": actor_id,
        "occurred_at": occurred_at.isoformat(timespec="microseconds"),
        "reason": reason,
    }


def _lock_recompile_predecessor(
    *,
    session: Session,
    rubric: models.Rubric,
    directive: dict | None,
    actor_id: str,
    reason: str,
) -> tuple[models.RubricCompilation | None, datetime | None]:
    active = session.scalars(
        select(models.RubricCompilation)
        .where(
            models.RubricCompilation.rubric_id == rubric.id,
            models.RubricCompilation.published_at.is_(None),
            models.RubricCompilation.status != "superseded",
        )
        .with_for_update()
    ).all()
    if directive is None:
        if active:
            raise ValueError(
                "draft already has an active compilation; explicit "
                "supersede_unpublished recompilation is required"
            )
        return None, None
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("draft recompilation reason must be non-empty")
    reason = reason.strip()
    if len(active) != 1:
        raise ValueError(
            "draft recompilation requires exactly one active unpublished predecessor"
        )
    predecessor = active[0]
    if predecessor.id != directive["supersedes_compilation_id"]:
        raise ValueError("draft recompilation predecessor is not the active compilation")
    if predecessor.published_at is not None:
        raise ValueError("a published compilation cannot be superseded")

    occurred_at = datetime.now(timezone.utc).replace(tzinfo=None)
    previous_status = predecessor.status
    predecessor.status = "superseded"
    predecessor.validation_result = {
        **deepcopy(dict(predecessor.validation_result or {})),
        "superseded": True,
    }
    predecessor.human_changes = [
        *deepcopy(list(predecessor.human_changes or [])),
        _recompile_event(
            action="supersede",
            before=previous_status,
            after="superseded",
            actor_id=actor_id,
            reason=reason,
            occurred_at=occurred_at,
        ),
    ]
    return predecessor, occurred_at


def persist_prepared_import(
    *,
    session: Session,
    prepared: Mapping[str, object] | PreparedRubricGraph,
    actor_id: str,
    organization_id: str | None = None,
    visibility: str = "private",
    target_rubric_id: str | None = None,
    reason: str = "explicit draft recompilation",
) -> PersistedImportIdentity:
    graph = prepared.to_mapping() if isinstance(prepared, PreparedRubricGraph) else _thaw(prepared)
    if not isinstance(graph, dict) or graph.get("schema_version") != PREPARED_SCHEMA_VERSION:
        raise ValueError("persist_prepared_import requires prepared-rubric-graph@1")
    try:
        with _short_transaction(session):
            actor = session.get(models.User, actor_id)
            if actor is None:
                raise ValueError("import actor does not exist")
            rubric_data = graph["rubric"]
            directive = _draft_recompile(graph.get("draft_recompile"))
            if target_rubric_id is None:
                if directive is not None:
                    raise ValueError(
                        "draft recompilation requires an existing target rubric"
                    )
                rubric = models.Rubric(
                    id=models.new_id(),
                    owner_id=actor_id,
                    organization_id=organization_id,
                    visibility=visibility,
                    name=rubric_data["name"],
                    version=rubric_data["version"],
                    total_score=_decimal(rubric_data["total_score"]),
                    status="draft",
                    description=rubric_data.get("description"),
                    format_spec=deepcopy(rubric_data.get("format_spec") or {}),
                    created_by=actor_id,
                )
                session.add(rubric)
            else:
                rubric = session.scalar(
                    select(models.Rubric)
                    .where(models.Rubric.id == target_rubric_id)
                    .with_for_update()
                )
                if rubric is None:
                    raise ValueError("target rubric does not exist")
                if rubric.status != "draft":
                    raise ValueError("only a draft rubric may receive an upgrade compilation")
                rubric.total_score = _decimal(rubric_data["total_score"])
                rubric.description = rubric_data.get("description")
                rubric.format_spec = deepcopy(rubric_data.get("format_spec") or {})

            projection_values = graph["legacy_projection"]["criteria"]
            if directive is not None:
                existing_codes = {item.code for item in rubric.criteria}
                incoming_codes = {
                    str(item["code"]) for item in projection_values
                }
                if existing_codes != incoming_codes:
                    raise ValueError(
                        "draft recompilation must preserve the complete criterion code set"
                    )

            predecessor, recompiled_at = _lock_recompile_predecessor(
                session=session,
                rubric=rubric,
                directive=directive,
                actor_id=actor_id,
                reason=reason,
            )

            criteria = _apply_projection(rubric, projection_values)
            compilation_data = graph["compilation"]
            provisional_hash = _digest_value(
                {
                    "scheme": graph["version"]["hash_scheme"],
                    "rubric": rubric_data,
                    "criteria": graph["criteria"],
                    "rules": graph["atomic_rules"],
                    "artifacts": graph["artifacts"],
                }
            )
            human_changes = deepcopy(compilation_data.get("human_changes") or [])
            validation_result = deepcopy(
                compilation_data.get("validation_result") or {}
            )
            if predecessor is not None and recompiled_at is not None:
                validation_result = {
                    **validation_result,
                    "supersedes_compilation_id": predecessor.id,
                }
                human_changes.append(
                    _recompile_event(
                        action="recompile",
                        before=predecessor.id,
                        after="new_compilation",
                        actor_id=actor_id,
                        reason=reason,
                        occurred_at=recompiled_at,
                    )
                )
            compilation = models.RubricCompilation(
                id=models.new_id(),
                rubric=rubric,
                status=compilation_data["status"],
                parser_version=compilation_data["parser_version"],
                compiler_version=compilation_data["compiler_version"],
                model_provider=compilation_data.get("model_provider"),
                model_name=compilation_data.get("model_name"),
                sampling_params=deepcopy(compilation_data.get("sampling_params") or {}),
                prompt_version=compilation_data["prompt_version"],
                raw_parse_output=deepcopy(compilation_data.get("raw_parse_output") or {}),
                raw_model_output=deepcopy(compilation_data.get("raw_model_output") or {}),
                validation_result=validation_result,
                blockers=deepcopy(compilation_data.get("blockers") or []),
                warnings=deepcopy(compilation_data.get("warnings") or []),
                human_changes=human_changes,
                created_by=actor_id,
                final_version_hash=provisional_hash,
            )
            session.add(compilation)
            version_data = graph["version"]
            version_collision = session.scalar(
                select(models.RubricVersion.id).where(
                    models.RubricVersion.rubric_id == rubric.id,
                    models.RubricVersion.version == version_data["version"],
                )
            )
            if version_collision is not None:
                raise ValueError("rubric version already exists")
            version = models.RubricVersion(
                id=models.new_id(),
                rubric_id=rubric.id,
                organization_id=rubric.organization_id,
                compilation=compilation,
                version=version_data["version"],
                workflow_profile=version_data["workflow_profile"],
                global_policy=deepcopy(version_data.get("global_policy") or {}),
                version_hash=provisional_hash,
                business_profile_key=version_data["business_profile_key"],
                hash_scheme=version_data["hash_scheme"],
                created_by=actor_id,
            )
            session.add(version)

            artifact_by_token: dict[str, models.SourceArtifact] = {}
            for value in graph["artifacts"]:
                artifact = models.SourceArtifact(
                    id=models.new_id(),
                    compilation=compilation,
                    artifact_type=value["artifact_type"],
                    file_name=value["file_name"],
                    file_hash=value["file_hash"],
                    file_size_bytes=int(value["file_size_bytes"]),
                    uploaded_by=actor_id,
                )
                session.add(artifact)
                artifact_by_token[value.get("token") or value["artifact_type"]] = artifact

            source_by_code: dict[str, models.SourceRule] = {}
            for value in graph["source_rules"]:
                artifact = artifact_by_token[value.get("artifact_token")]
                source = models.SourceRule(
                    id=models.new_id(),
                    artifact=artifact,
                    source_rule_code=value["source_rule_code"],
                    sheet_name=value["sheet_name"],
                    row_number=int(value["row_number"]),
                    cell_locator=value["cell_locator"],
                    raw_text=value["raw_text"],
                )
                session.add(source)
                source_by_code[value["source_rule_code"]] = source

            template_by_code: dict[str, models.TemplateItem] = {}
            for value in graph["template_items"]:
                item = models.TemplateItem(
                    id=models.new_id(),
                    artifact=artifact_by_token[value.get("artifact_token")],
                    item_code=value["item_code"],
                    kind=value["kind"],
                    section_path=deepcopy(value.get("section_path") or []),
                    raw_text=value["raw_text"],
                    normalized_constraint=deepcopy(value.get("normalized_constraint")),
                    strictness=value["strictness"],
                    source_locator=deepcopy(value.get("source_locator") or {}),
                    source_hash=value["source_hash"],
                    parse_confidence=_decimal(value.get("parse_confidence", "1")),
                )
                session.add(item)
                template_by_code[value["item_code"]] = item

            rule_by_code: dict[str, models.AtomicRule] = {}
            for value in graph["atomic_rules"]:
                criterion = criteria[value["criterion_code"]]
                rule = models.AtomicRule(
                    id=models.new_id(),
                    rubric_version=version,
                    criterion=criterion,
                    rule_code=value["rule_code"],
                    name=value["name"],
                    rule_text=value["rule_text"],
                    direction=value["direction"],
                    effect_type=value["effect_type"],
                    max_points=None if value.get("max_points") is None else _decimal(value["max_points"]),
                    repeat_policy=value.get("repeat_policy"),
                    cap_points=None if value.get("cap_points") is None else _decimal(value["cap_points"]),
                    judge_type=value["judge_type"],
                    checker_key=value.get("checker_key"),
                    checker_params=deepcopy(value.get("checker_params") or {}),
                    evidence_policy=deepcopy(value.get("evidence_policy") or {}),
                    positive_example=value.get("positive_example"),
                    negative_example=value.get("negative_example"),
                    boundary_example=value.get("boundary_example"),
                    strictness=value["strictness"],
                    applies_to=value["applies_to"],
                    mutex_group=value.get("mutex_group"),
                    depends_on_rule_codes=deepcopy(value.get("depends_on_rule_codes") or []),
                    status="draft",
                    creation_method=value["creation_method"],
                    reviewed_by=None,
                    reviewed_at=None,
                )
                for source_code in value.get("source_rule_codes") or []:
                    rule.source_rules.append(source_by_code[source_code])
                for level in value.get("levels") or []:
                    rule.levels.append(
                        models.RuleLevel(
                            id=models.new_id(),
                            level_code=level["level_code"],
                            points=_decimal(level["points"]),
                            descriptor=level["descriptor"],
                            positive_example=level.get("positive_example"),
                            negative_example=level.get("negative_example"),
                            display_order=int(level.get("display_order", 0)),
                        )
                    )
                session.add(rule)
                rule_by_code[rule.rule_code] = rule

            for value in graph["template_links"]:
                link = models.RuleTemplateLink(
                    id=models.new_id(),
                    rule=rule_by_code[value["rule_code"]],
                    template_item=template_by_code[value["template_item_code"]],
                    relationship_type=value["relationship_type"],
                    match_method=value["match_method"],
                    match_confidence=(None if value.get("match_confidence") is None else _decimal(value["match_confidence"])),
                    rationale=value["rationale"],
                    review_status=value.get("review_status") or "pending",
                    reviewed_by=None,
                    reviewed_at=None,
                )
                session.add(link)
            session.flush()
            identity = PersistedImportIdentity(rubric.id, compilation.id, version.id)
        return identity
    except Exception:
        session.rollback()
        raise


@contextmanager
def _short_transaction(session: Session):
    """Own and close the import transaction, including SQLAlchemy autobegin.

    Reading an expired ``actor.id`` while evaluating the call can autobegin a
    clean transaction.  Reusing that transaction avoids a second BEGIN while
    retaining the service's commit/rollback ownership contract.
    """

    if session.new or session.dirty or session.deleted:
        raise ValueError("persist_prepared_import requires a clean session")
    if not session.in_transaction():
        session.begin()
    try:
        yield
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()


def rebuild_legacy_projection(*, session: Session, rubric_id: str) -> models.Rubric:
    rubric = session.get(models.Rubric, rubric_id)
    if rubric is None:
        raise ValueError("rubric does not exist")
    compilations = session.scalars(
        select(models.RubricCompilation)
        .where(
            models.RubricCompilation.rubric_id == rubric_id,
            models.RubricCompilation.status != "superseded",
        )
        .order_by(models.RubricCompilation.created_at.desc(), models.RubricCompilation.id.desc())
    ).all()
    if not compilations:
        raise ValueError("rubric has no authoritative provenance graph")
    published = [item for item in compilations if item.published_at is not None]
    candidates = published or [
        item for item in compilations if item.published_at is None
    ]
    if len(candidates) != 1:
        raise ValueError("rubric active provenance graph is ambiguous")
    compilation = candidates[0]
    versions = session.scalars(
        select(models.RubricVersion).where(
            models.RubricVersion.compilation_id == compilation.id
        )
    ).all()
    if len(versions) != 1:
        raise ValueError("active compilation must have exactly one rubric version")
    version = versions[0]

    parsed_criteria = deepcopy(
        (compilation.raw_parse_output or {}).get("criteria") or []
    )
    if not parsed_criteria:
        raise ValueError("compilation does not contain an authoritative criterion projection")
    base_by_code = {
        str(item["code"]): _criterion_projection(item)
        for item in parsed_criteria
        if isinstance(item, Mapping) and item.get("code")
    }
    current_by_id = {
        item.id: item
        for item in session.scalars(
            select(models.RubricCriterion).where(
                models.RubricCriterion.rubric_id == rubric_id
            )
        ).all()
    }
    rules = session.scalars(
        select(models.AtomicRule)
        .where(models.AtomicRule.rubric_version_id == version.id)
        .order_by(models.AtomicRule.rule_code)
    ).all()
    rules_by_code: dict[str, list[models.AtomicRule]] = {}
    for rule in rules:
        criterion = current_by_id.get(rule.criterion_id)
        if criterion is None:
            raise ValueError("active rule points outside the rubric criteria")
        rules_by_code.setdefault(criterion.code, []).append(rule)

    projections: list[dict] = []
    for code, base in base_by_code.items():
        criterion_rules = rules_by_code.get(code, [])
        band_rules = [
            item
            for item in criterion_rules
            if item.direction == "band" and item.effect_type == "score"
        ]
        deduct_rules = [
            item
            for item in criterion_rules
            if item.direction == "deduct" and item.effect_type == "score"
        ]
        review_rules = [
            item
            for item in criterion_rules
            if item.direction == "none" and item.effect_type == "review"
        ]
        judge_types = {item.judge_type for item in criterion_rules}
        if judge_types == {"deterministic"}:
            base["criterion_type"] = "deterministic"
        elif len(judge_types) > 1:
            base["criterion_type"] = "hybrid"
        elif judge_types == {"semantic"}:
            base["criterion_type"] = "llm_judgment"

        if band_rules:
            # A publishable graph has exactly one band rule.  During draft
            # repair we still project the stable first rule so the UI exposes
            # the current authoritative edit instead of stale parse output.
            band_rule = band_rules[0]
            levels = sorted(
                list(band_rule.levels or []),
                key=lambda item: (item.display_order, item.level_code),
            )
            base["scoring_mode"] = "banded"
            base["rubric_levels"] = [
                {
                    "level_code": item.level_code,
                    "label": item.descriptor or item.level_code,
                    "points": _number_text(item.points),
                    "descriptor": item.descriptor,
                    "positive_example": item.positive_example,
                    "negative_example": item.negative_example,
                    "display_order": item.display_order,
                }
                for item in levels
            ]
            base["deduction_rules_structured"] = []
        elif deduct_rules:
            base["scoring_mode"] = "deductive"
            base["rubric_levels"] = []
            base["deduction_rules_structured"] = [
                {
                    "rule_code": item.rule_code,
                    "rule_text": item.rule_text,
                    "points": _number_text(item.max_points),
                    "repeat_policy": item.repeat_policy,
                    "cap_points": (
                        None
                        if item.cap_points is None
                        else _number_text(item.cap_points)
                    ),
                    "checker_key": item.checker_key,
                    "checker_params": deepcopy(item.checker_params or {}),
                    "source": "atomic_rule",
                }
                for item in deduct_rules
            ]
        elif review_rules:
            base["scoring_mode"] = "review_only"
            base["rubric_levels"] = []
            base["deduction_rules_structured"] = []
        projections.append(base)

    _apply_projection(rubric, projections)
    session.flush()
    return rubric


__all__ = [
    "IMPORT_SCHEMA_VERSION",
    "PREPARED_SCHEMA_VERSION",
    "PersistedImportIdentity",
    "PreparedRubricGraph",
    "persist_prepared_import",
    "prepare_file_import",
    "prepare_legacy_draft_upgrade",
    "prepare_manual_json_import",
    "prepare_manual_json_recompile",
    "rebuild_legacy_projection",
]
