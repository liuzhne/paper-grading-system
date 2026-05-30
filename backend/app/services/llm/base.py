from abc import ABC
from abc import abstractmethod


class LLMScorer(ABC):
    provider = "abstract"
    model_name = "abstract"
    model_version = "v1"

    @abstractmethod
    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks, anchors=None):
        raise NotImplementedError

    def complete_json(self, instructions, payload):
        """通用结构化 JSON 补全原语（供语义一致性、L2 等复用）。默认未实现。"""
        raise NotImplementedError


class LLMScoringError(RuntimeError):
    pass
