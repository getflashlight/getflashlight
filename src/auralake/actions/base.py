"""Base class for mutating actions."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from auralake.core.context import ExecutionContext
from auralake.models.recommendations import Recommendation


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AbstractAction(ABC):
    """Base class for actions that modify resources."""

    name: str
    risk_level: RiskLevel

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    @abstractmethod
    def execute(self, recommendation: Recommendation) -> None:
        """Execute the action described by the recommendation."""
        ...

    @abstractmethod
    def rollback(self, recommendation: Recommendation) -> None:
        """Attempt to rollback the action."""
        ...

    def validate(self, recommendation: Recommendation) -> bool:
        """Validate that the action can be safely performed."""
        return True
