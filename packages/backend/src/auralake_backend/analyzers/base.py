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

    def rule_enabled(self, rule_id: str) -> bool:
        """Check if a named rule is enabled in config."""
        rule_cfg = getattr(self.context.config.rules, rule_id, None)
        return rule_cfg.enabled if rule_cfg else True

    def rule_threshold(self, rule_id: str, key: str, default: float | int) -> float | int:
        """Get per-rule threshold override, falling back to default."""
        rule_cfg = getattr(self.context.config.rules, rule_id, None)
        if rule_cfg and key in rule_cfg.thresholds:
            return type(default)(rule_cfg.thresholds[key])
        return default

    def pricing_basis(self) -> str:
        """Return 'negotiated' if any discounts are configured, else 'list'."""
        disc = self.context.config.databricks.discounts
        if (
            disc.databricks.global_dbu_discount_pct > 0
            or disc.databricks.sku_overrides
            or disc.aws.edp_discount_pct > 0
        ):
            return "negotiated"
        return "list"
