"""Business profiles that adapt domain data and extend the generic Core."""

from backend.app.services.scoring.profiles.technical_proposal import (
    TechnicalProposalProfile,
)
from backend.app.services.scoring.profiles.thesis import ThesisProfile


__all__ = ["TechnicalProposalProfile", "ThesisProfile"]
