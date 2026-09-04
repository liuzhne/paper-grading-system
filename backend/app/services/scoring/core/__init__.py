"""与业务 Profile、ORM 和传输层无关的评分 Core 原语。"""

from backend.app.services.scoring.core.contracts import (
    AtomicRuleSnapshot,
    CompiledRubricSnapshot,
    DeterministicCheckerResultV1,
    DocumentSnapshot,
    PromptEnvelopeV1,
    PromptEnvelopeV2,
    PromptEnvelopeV3,
    PromptEnvelopeV4,
    RuleExecutionPlan,
    ScoringRequest,
    SemanticRuleResponseV2,
    SubmissionSnapshot,
)
from backend.app.services.scoring.core.evidence import detect_injection
from backend.app.services.scoring.core.engine import score_submission
from backend.app.services.scoring.core.identity import (
    derive_evidence_unit_id,
    hash_document_snapshot,
    hash_normalized_content,
    hash_source_artifact,
)
from backend.app.services.scoring.core.ports import (
    CacheLedger,
    CheckerRegistry,
    Clock,
    EvidenceRetriever,
    LLMRuntime,
)
from backend.app.services.scoring.core.results import RuleExecutionResult, ScoringOutcome
from backend.app.services.scoring.core.rule_executor import execute_rule_plan


__all__ = [
    "AtomicRuleSnapshot",
    "CacheLedger",
    "CheckerRegistry",
    "Clock",
    "CompiledRubricSnapshot",
    "DocumentSnapshot",
    "DeterministicCheckerResultV1",
    "EvidenceRetriever",
    "LLMRuntime",
    "PromptEnvelopeV1",
    "PromptEnvelopeV2",
    "PromptEnvelopeV3",
    "PromptEnvelopeV4",
    "RuleExecutionPlan",
    "RuleExecutionResult",
    "ScoringOutcome",
    "ScoringRequest",
    "SemanticRuleResponseV2",
    "SubmissionSnapshot",
    "detect_injection",
    "derive_evidence_unit_id",
    "execute_rule_plan",
    "hash_document_snapshot",
    "hash_normalized_content",
    "hash_source_artifact",
    "score_submission",
]
