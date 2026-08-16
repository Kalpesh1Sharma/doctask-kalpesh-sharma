"""Provider-neutral immutable Rule-evaluation boundary."""

from typing import Protocol

from domain.rule_evaluation import RuleEvaluationRequest, RuleEvaluationResponse


class RuleEvaluationProvider(Protocol):
    def evaluate(self, request: RuleEvaluationRequest) -> RuleEvaluationResponse:
        """Return strict data-only outcomes for every supplied Rule."""


class RuleEvaluationProviderError(RuntimeError):
    """Raised when a provider cannot complete a Rule-evaluation request."""


class RuleEvaluationInvalidResponseError(RuleEvaluationProviderError):
    """Raised when a provider returns unusable structured output."""
