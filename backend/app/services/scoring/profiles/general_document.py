"""通用文档 Profile：评任意类型的文档（方案、报告、作业等）。

继承论文 Profile 的全部机制——解析适配、证据切块、检查器实现、分组调用、单篇输入
上限、用量计量与证据信封——只替换与“论文”绑定的几项（通用文档评分改造方案 §4.2）：

- 提示词扩展的领域说明为 ``document``，送入提示词的元数据只有标题；
- 不注册“研究问题与结论呼应”检查器，也不跑按绪论/结论写的模型语义一致性核验；
- 表格与报告的身份字段是文件名、标题、归属单位、类别，而不是学号、姓名、学院、专业。

扣哪项、扣几分仍然只来自用户发布的评分标准。
"""

from __future__ import annotations

from backend.app.services.scoring.profiles.thesis import THESIS_CHECKER_KEYS
from backend.app.services.scoring.profiles.thesis import ThesisProfile
from backend.app.services.scoring.profiles.thesis_artifacts import (
    ThesisArtifactFacade,
)


GENERAL_DOCUMENT_PROFILE_KEY = "general_document"
GENERAL_DOCUMENT_PROFILE_VERSION = "general-document-profile@1"
GENERAL_DOCUMENT_PROMPT_VERSION = "general-document-2026-10-11"
GENERAL_DOCUMENT_CHECKER_KEYS = tuple(
    key
    for name, key in THESIS_CHECKER_KEYS.items()
    if name != "research_conclusion"
) + ("thesis.legacy_required_fields.v1",)


class GeneralDocumentArtifactFacade(ThesisArtifactFacade):
    """论文投影之上补文件名：通用文档没有学号，文件名是唯一一定有的标识。"""

    profile_key = GENERAL_DOCUMENT_PROFILE_KEY

    def build_artifact_projection(self, **kwargs):
        projection = super().build_artifact_projection(**kwargs)
        projection["paper"]["file_name"] = kwargs["run"].paper.file_name
        return projection

    def build_spreadsheet_projection(self, *, batch, run, review_logs):
        projection = super().build_spreadsheet_projection(
            batch=batch, run=run, review_logs=review_logs
        )
        file_name = run.paper.file_name
        projection["schema_version"] = "general-document-spreadsheet-projection@1"
        projection["summary"]["file_name"] = file_name
        for item in projection["details"]:
            item["file_name"] = file_name
        return projection


class GeneralDocumentProfile(ThesisProfile):
    profile_key = GENERAL_DOCUMENT_PROFILE_KEY
    profile_version = GENERAL_DOCUMENT_PROFILE_VERSION
    prompt_version = GENERAL_DOCUMENT_PROMPT_VERSION
    prompt_domain = "document"
    prompt_metadata_fields = frozenset({"title"})
    excluded_checker_keys = frozenset({THESIS_CHECKER_KEYS["research_conclusion"]})
    semantic_coherence = False
    spreadsheet_summary_identity = (
        ("文件名", "file_name"),
        ("标题", "title"),
        ("归属单位", "department"),
        ("类别", "major"),
    )
    spreadsheet_detail_identity = (
        ("文件名", "file_name"),
        ("标题", "title"),
    )
    report_labels = {
        "page_title": "材料评分报告",
        "heading": "材料评分报告",
        "title": "标题",
        "owner": "文件",
    }

    def __init__(self):
        self._artifacts = GeneralDocumentArtifactFacade()


__all__ = [
    "GENERAL_DOCUMENT_CHECKER_KEYS",
    "GENERAL_DOCUMENT_PROFILE_KEY",
    "GENERAL_DOCUMENT_PROFILE_VERSION",
    "GENERAL_DOCUMENT_PROMPT_VERSION",
    "GeneralDocumentProfile",
]
