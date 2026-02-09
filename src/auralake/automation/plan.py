"""Execution plan for batching actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from auralake.models.recommendations import Recommendation


@dataclass
class ExecutionStep:
    recommendation: Recommendation
    action_fn: Callable[[], None]


@dataclass
class ExecutionPlan:
    name: str
    steps: list[ExecutionStep] = field(default_factory=list)

    def add_step(self, recommendation: Recommendation, action_fn: Callable[[], None]) -> None:
        self.steps.append(ExecutionStep(recommendation=recommendation, action_fn=action_fn))

    @property
    def total_estimated_savings(self):
        return sum(s.recommendation.estimated_monthly_savings_usd for s in self.steps)
