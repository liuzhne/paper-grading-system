import inspect
from types import SimpleNamespace

import pytest

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.profiles import thesis as thesis_module
from backend.app.services.scoring.profiles.thesis import THESIS_CHECKER_KEYS
from backend.app.services.scoring.profiles.thesis import ThesisProfile
from backend.app.services.rubrics.executable_validator import (
    DEFAULT_CHECKER_REGISTRY,
)


def _document(text="研究问题是什么。\n结论回答研究问题。"):
    evidence_id = canonical_sha256({"text": text})
    return {
        "schema_version": "document-snapshot@1",
        "full_text": text,
        "sections": [
            {
                "section_path": ["摘要"],
                "section_ordinal": 0,
                "heading": "摘要",
                "normalized_text": "研究问题是什么。",
                "evidence_unit_ids": [evidence_id],
                "locator": {
                    "kind": "section",
                    "section_path": ["摘要"],
                    "section_ordinal": 0,
                },
            },
            {
                "section_path": ["结论"],
                "section_ordinal": 1,
                "heading": "结论",
                "normalized_text": "结论回答研究问题。",
                "evidence_unit_ids": [evidence_id],
                "locator": {
                    "kind": "section",
                    "section_path": ["结论"],
                    "section_ordinal": 1,
                },
            },
        ],
        "evidence_units": [],
        "metrics": {"character_count": len(text)},
        "format_facts": {"body_font_size_pt": 12},
        "profile_extensions": {},
    }


def _observation(result):
    assert result["schema_version"] == "deterministic-checker-result@1"
    assert result["observations"]
    return result["observations"][0]


def test_thesis_profile_registers_only_explicit_versioned_checker_routes():
    profile = ThesisProfile()
    registry = profile.build_checker_registry()
    manifest = registry.manifest(
        profile_key=profile.profile_key,
        document_schema_version="document-snapshot@1",
    )

    assert set(THESIS_CHECKER_KEYS.values()).issubset(manifest)
    assert len(THESIS_CHECKER_KEYS) == 6
    assert all(key.startswith("thesis.") and ".v" in key for key in manifest)
    assert "3000" not in inspect.getsource(thesis_module)


def test_word_count_checker_requires_user_parameters_and_has_no_default():
    profile = ThesisProfile()
    registry = profile.build_checker_registry()
    key = THESIS_CHECKER_KEYS["text_length"]

    with pytest.raises(ValueError, match="minimum_chars"):
        registry.resolve(
            checker_key=key,
            checker_version="1.0.0",
            checker_params={},
            profile_key="thesis",
            document_schema_version="document-snapshot@1",
        )

    checker = registry.resolve(
        checker_key=key,
        checker_version="1.0.0",
        checker_params={"minimum_chars": 5, "maximum_chars": 100},
        profile_key="thesis",
        document_schema_version="document-snapshot@1",
    )
    passing = _observation(
        checker(
            document=_document("123456"),
            params={"minimum_chars": 5, "maximum_chars": 100},
        )
    )
    failing = _observation(
        checker(
            document=_document("1234"),
            params={"minimum_chars": 5, "maximum_chars": 100},
        )
    )

    assert passing["status"] == "not_triggered"
    assert failing["status"] == "triggered"
    assert failing["expected_value"] == {"minimum_chars": 5, "maximum_chars": 100}


@pytest.mark.parametrize(
    ("route", "params"),
    [
        ("structure", {"required_sections": ["摘要", "结论"]}),
        ("figure_references", {"minimum_references": 1}),
        ("citations", {"require_bibliography": True}),
        ("format", {"required_facts": {"body_font_size_pt": 12}}),
        (
            "research_conclusion",
            {
                "research_section_markers": ["摘要"],
                "conclusion_section_markers": ["结论"],
            },
        ),
    ],
)
def test_checker_dispatch_is_independent_of_criterion_display_name(route, params):
    profile = ThesisProfile()
    registry = profile.build_checker_registry()
    key = THESIS_CHECKER_KEYS[route]
    checker = registry.resolve(
        checker_key=key,
        checker_version="1.0.0",
        checker_params=params,
        profile_key="thesis",
        document_schema_version="document-snapshot@1",
    )

    first = checker(document=_document("正文引用图1，并在正文使用[1]。\n参考文献\n[1] 文献。"), params=params)
    second = checker(document=_document("正文引用图1，并在正文使用[1]。\n参考文献\n[1] 文献。"), params=params)

    assert first == second
    assert _observation(first)["status"] in {
        "triggered",
        "not_triggered",
        "not_applicable",
    }


def test_profile_owns_paper_snapshot_extensions_and_prompt_metadata():
    profile = ThesisProfile()
    paper = SimpleNamespace(
        id="paper-1",
        student_id="S001",
        student_name="测试学生",
        title="测试论文",
        department="软件学院",
        major="软件工程",
        advisor="测试教师",
        parse_quality=0.99,
    )
    parsed = {
        "full_text": "摘要\n研究问题。\n结论\n回答研究问题。",
        "sections": [
            {
                "title": "摘要",
                "level": 1,
                "paragraphs": [{"text": "研究问题。"}],
            },
            {
                "title": "结论",
                "level": 1,
                "paragraphs": [{"text": "回答研究问题。"}],
            },
        ],
        "structure_checks": [{"code": "HAS_ABSTRACT", "passed": True}],
        "references": [{"number": 1, "text": "测试参考文献"}],
        "coherence_findings": [{"kind": "research_conclusion", "severity": "info"}],
        "format_findings": [{"kind": "font", "severity": "warning"}],
        "format_facts": {"body_font_size_pt": 12},
        "parse_quality": 0.99,
    }

    snapshots = profile.adapt_paper(
        paper=paper,
        parsed=parsed,
        chunks=(),
        source_artifact_hash="a" * 64,
        submission_instance_key=paper.id,
    )
    submission = snapshots.submission.to_mapping()
    document = snapshots.document.to_mapping()
    extensions = profile.build_prompt_extensions(
        submission_snapshot=submission,
        document_snapshot=document,
    )

    assert submission["profile_key"] == "thesis"
    assert document["profile_version"] == profile.profile_version
    assert document["profile_extensions"]["thesis"]["structure_checks"]
    assert document["profile_extensions"]["thesis"]["references"] == [
        {"number": 1, "text": "测试参考文献"}
    ]
    assert document["profile_extensions"]["thesis"]["coherence_findings"]
    assert extensions["metadata"]["student_id"] == "S001"
    assert extensions["profile_extensions"]["format_findings"]


def test_profile_owns_mock_runtime_and_replay_identity():
    profile = ThesisProfile()
    scorer = SimpleNamespace(
        provider="mock",
        model_name="fixed-mock",
        model_version="m5",
    )
    runtime = profile.build_llm_runtime(scorer)
    identity = profile.build_runtime_identity(scorer)
    envelope = SimpleNamespace(
        to_mapping=lambda: {
            "atomic_rule_snapshot": {
                "rule_code": "RULE-1",
                "levels": [
                    {
                        "level_code": "PASS",
                        "display_order": 0,
                    }
                ],
            },
            "evidence_units": [
                {
                    "evidence_unit_id": "evidence-1",
                    "normalized_text": "可核验证据",
                    "section_path": ["正文"],
                }
            ],
        }
    )

    response = runtime.score(envelope=envelope)

    assert response["rule_code"] == "RULE-1"
    assert response["level_code"] == "PASS"
    assert identity["profile_key"] == profile.profile_key
    assert identity["profile_version"] == profile.profile_version
    assert identity["prompt_version"] == profile.prompt_version


def test_legacy_engine_uses_public_thesis_profile_facade():
    from backend.app.services.scoring import engine

    assert engine.ThesisProfile is ThesisProfile
    assert "_LegacyThesisProfile" not in vars(engine)
    assert "_LegacyScorerRuntime" not in vars(engine)


def test_publication_registry_accepts_profile_checker_keys_and_params():
    params_by_route = {
        "structure": {"required_sections": ["摘要", "结论"]},
        "text_length": {"minimum_chars": 5000, "maximum_chars": 50000},
        "figure_references": {"minimum_references": 1},
        "citations": {"require_bibliography": True},
        "format": {"required_facts": {"body_font_size_pt": 12}},
        "research_conclusion": {
            "research_section_markers": ["摘要", "研究问题"],
            "conclusion_section_markers": ["结论"],
        },
    }

    for route, key in THESIS_CHECKER_KEYS.items():
        registration = DEFAULT_CHECKER_REGISTRY.publication_registration(key)
        assert registration is not None
        assert registration["checker_version"] == "1.0.0"
        assert "thesis" in registration["supported_profiles"]
        DEFAULT_CHECKER_REGISTRY.validate_publication_params(
            key,
            params_by_route[route],
        )
