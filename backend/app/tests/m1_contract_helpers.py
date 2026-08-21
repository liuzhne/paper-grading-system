"""Shared builders for the M1 executable contracts.

The production M1 modules do not exist on the M0 branch yet.  The helpers keep
the tests focused on observable contracts and deliberately avoid importing ORM
models, opening files, or contacting a provider.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from backend.app.services.cache import llm_cache


EVIDENCE_UNIT_1 = "a" * 64
EVIDENCE_UNIT_2 = "b" * 64


def observation_payload(**content) -> dict:
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {**content, "payload_hash": hashlib.sha256(encoded).hexdigest()}


def refresh_observation_hash(observation: dict) -> None:
    content = {key: value for key, value in observation.items() if key != "payload_hash"}
    observation.update(observation_payload(**content))


def prompt_envelope_payload() -> dict:
    """Return a complete, provider-ready PromptEnvelopeV1 fixture."""

    calibration_anchors = [
        {
            "label": "优",
            "score": "9",
            "max_score": "10",
            "excerpt": "范文明确说明了数据来源和回归分析步骤。",
            "rationale": "方法证据完整。",
        }
    ]
    calibration_anchors_hash = hashlib.sha256(
        json.dumps(
            calibration_anchors,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "prompt-envelope@1",
        "prompt_version": llm_cache.PROMPT_VERSION,
        "profile": {
            "key": "thesis",
            "prompt_version": "thesis-criterion-v1",
        },
        "engine": {"version": "legacy-m1-adapter-v1"},
        "provider": {
            "name": "mock",
            "model": "mock-criterion-scorer",
            "model_version": "v1",
            "sampling": {
                "temperature": "0",
                "top_p": "1",
                "seed": 20260718,
                "max_tokens": 512,
            },
            "thinking": {"enabled": False, "type": None},
            "response_format": "none",
            "response_schema": "criterion-score-v2",
        },
        "rubric_snapshot_hash": "1" * 64,
        "policy_hash": "2" * 64,
        "criterion": {
            "code": "C01",
            "name": "研究方法",
            "max_score": "10",
            "scoring_mode": "deductive",
            "description": "评价研究设计、数据来源与分析方法。",
            "evidence_hints": ["数据来源", "分析方法"],
            "deduction_rules": ["未说明数据来源时扣分"],
            "rubric_levels": [],
            "authorized_rules": [
                {
                    "code": "LEGACY:C01:" + "a" * 64,
                    "points": "2",
                    "description": "未说明数据来源",
                    "evidence_mode": "source_quote",
                    "absence_target": None,
                }
            ],
        },
        "submission": {
            "title": "基于可解释模型的教学质量评价",
            "source_artifact_hash": "3" * 64,
            "normalized_content_hash": "4" * 64,
            "document_snapshot_hash": "5" * 64,
        },
        "evidence_units": [
            {
                "evidence_unit_id": EVIDENCE_UNIT_2,
                "text": "第三章说明了问卷来源和回归分析方法。",
                "location": "第三章/3.2",
                "section_title": "3.2 数据与方法",
            },
            {
                "evidence_unit_id": EVIDENCE_UNIT_1,
                "text": "摘要概述了研究问题。",
                "location": "摘要",
                "section_title": "中文摘要",
            },
        ],
        "observations": [
            observation_payload(
                checker_key="thesis.structure.required_sections.v1",
                checker_version="1.0.0",
                locator={
                    "kind": "section",
                    "section_path": ["第三章", "3.2 数据与方法"],
                    "section_ordinal": 3,
                },
                observation_code="METHOD_SECTION_PRESENT",
                measured_value=True,
                expected_value=True,
            ),
            observation_payload(
                checker_key="thesis.references.required_section.v1",
                checker_version="1.0.0",
                locator={
                    "kind": "section",
                    "section_path": ["参考文献"],
                    "section_ordinal": 8,
                },
                observation_code="REFERENCE_SECTION_PRESENT",
                measured_value=True,
                expected_value=True,
            ),
        ],
        "calibration_anchors": calibration_anchors,
        "calibration_anchors_hash": calibration_anchors_hash,
        "coverage": {
            "declared_scope": ["摘要", "第三章"],
            "scope_selector": "retrieval://candidate-units",
            "authoritative_for_absence": False,
            "expected_evidence_unit_ids": [EVIDENCE_UNIT_2, EVIDENCE_UNIT_1],
            "checked_evidence_unit_ids": [EVIDENCE_UNIT_2, EVIDENCE_UNIT_1],
            "completeness": "complete",
            "evidence_policy_version": "thesis-evidence-v1",
        },
    }


def changed_prompt_envelope(path: tuple[str | int, ...], value) -> dict:
    payload = prompt_envelope_payload()
    cursor = payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    return payload


def deep_copy(value):
    return deepcopy(value)
