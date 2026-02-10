"""Abstract base class for all analyzers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import AnalysisResult


class AbstractAnalyzer(ABC):
    """Base class for analyzers that produce cost recommendations."""

    name: str

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    @abstractmethod
    def analyze(self) -> AnalysisResult:
        """Run analysis and return results with recommendations."""
        ...
