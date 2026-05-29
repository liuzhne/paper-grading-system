from abc import ABC
from abc import abstractmethod


class LLMScorer(ABC):
    provider = "abstract"
    model_name = "abstract"
    model_version = "v1"

    @abstractmethod
    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks):
        raise NotImplementedError


class LLMScoringError(RuntimeError):
    pass
