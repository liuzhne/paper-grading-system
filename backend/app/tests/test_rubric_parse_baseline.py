"""阶段 0：解析结果快照基线。

重构解析内核时，评分项、原子规则、来源规则（除 cell_locator 外）与模板条目必须保持不变；
快照文件由重构前的实现生成。确需改变时，先在 DECISIONS.md 记录，再以
`UPDATE_RUBRIC_PARSE_SNAPSHOTS=1` 重新生成并在评审中说明差异。
"""

import json
import os
from pathlib import Path

import pytest

from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.parser import parse_rubric_files
from backend.app.tests import rubric_parse_fixtures as fx

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "rubric_parse"

CASES = {
    "complex": (fx.complex_rules_xlsx, None),
    "simple": (fx.simple_rules_xlsx, None),
    "simple_with_template": (fx.simple_rules_xlsx, fx.template_docx_with_comments),
}


def _command(template):
    return {
        "schema_version": pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "file_import",
        "rubric": {"name": "快照", "version": "v1", "business_profile_key": "thesis"},
        "files": {"rules_file_name": "rules.xlsx", "template_file_name": "t.docx" if template else None},
        "compiler": {"parser_version": "p", "compiler_version": "c", "prompt_version": "none"},
        "version": {"hash_scheme": "rubric-content-v2"},
    }


def _projection(rules_factory, template_factory):
    template = template_factory() if template_factory else None
    graph = pipeline.prepare_file_import(
        command=_command(template), rules_bytes=rules_factory(), template_bytes=template
    ).to_mapping()
    legacy = parse_rubric_files(rules_bytes=rules_factory(), template_bytes=template, scorer=None)
    return {
        "criteria": graph["criteria"],
        "atomic_rules": graph["atomic_rules"],
        "source_rules": [
            {k: v for k, v in item.items() if k != "cell_locator"} for item in graph["source_rules"]
        ],
        "template_items": graph["template_items"],
        "template_links": graph["template_links"],
        "blockers": graph["compilation"]["blockers"],
        "legacy_criteria": [
            {"code": c.code, "name": c.name, "max_score": c.max_score, "deduction_rules": c.deduction_rules}
            for c in legacy.criteria
        ],
    }


@pytest.mark.parametrize("case", sorted(CASES))
def test_parse_output_matches_snapshot(case):
    actual = json.loads(json.dumps(_projection(*CASES[case]), ensure_ascii=False, sort_keys=True, default=str))
    path = SNAPSHOT_DIR / f"{case}.json"
    if os.environ.get("UPDATE_RUBRIC_PARSE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"缺少快照 {path}"
    assert actual == json.loads(path.read_text(encoding="utf-8"))


def test_complex_fixture_documents_remaining_scope():
    """重构前，合并整行的表尾全局规则被误当作满分 0.5 的评分项（阶段 3 已修复，
    快照于阶段 3 按 DECISIONS 记录重新生成）。仍保留的边界：只解析第一张表、
    无满分行不进入评分项——二者改为进入覆盖率报告，由用户处理。"""

    graph = pipeline.prepare_file_import(
        command=_command(None), rules_bytes=fx.complex_rules_xlsx(), template_bytes=None
    ).to_mapping()
    names = [item["name"] for item in graph["criteria"]]
    assert names == ["选题意义", "文献综述", "方法设计"]
    raw = json.dumps(graph["source_rules"], ensure_ascii=False)
    assert "学术诚信" not in raw  # 第二张工作表未读
    assert "表述要严谨" not in raw  # 无满分行丢失
