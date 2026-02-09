"""Progressive automation engine.

Handles the execution lifecycle: recommend -> dry-run -> apply -> auto -> PR.
"""
from __future__ import annotations

from auralake.core.context import ExecutionContext
from auralake.core.output import confirm_action, print_success, print_warning
from auralake.models.config import AutomationLevel
from auralake.models.recommendations import Recommendation, RiskLevel
from auralake.automation.plan import ExecutionPlan
from auralake.automation.audit import AuditTrail
from auralake.core.exceptions import SafetyError


class AutomationEngine:
    """Orchestrates action execution based on automation level."""

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self.audit = AuditTrail(context)

    def execute(self, plan: ExecutionPlan) -> list[dict]:
        """Execute an action plan according to the automation level."""
        level = self.context.automation_level
        results = []

        for step in plan.steps:
            rec = step.recommendation
            action_fn = step.action_fn

            if level == AutomationLevel.RECOMMEND:
                results.append({"action": rec.title, "status": "recommended", "recommendation": rec})
                continue

            if level == AutomationLevel.DRY_RUN:
                results.append({"action": rec.title, "status": "dry_run", "would_change": rec.recommended_state})
                continue

            if level == AutomationLevel.APPLY:
                if not confirm_action(f"Apply: {rec.title}? (saves ~${rec.estimated_monthly_savings_usd}/mo)"):
                    self.audit.record(rec, "skipped", "User declined")
                    results.append({"action": rec.title, "status": "skipped"})
                    continue

            if level == AutomationLevel.AUTO:
                self._check_safety(rec)

            # Execute the action
            self.audit.record(rec, "started")
            try:
                action_fn()
                self.audit.record(rec, "completed")
                results.append({"action": rec.title, "status": "applied"})
                print_success(f"Applied: {rec.title}")
            except Exception as exc:
                self.audit.record(rec, "failed", str(exc))
                results.append({"action": rec.title, "status": "failed", "error": str(exc)})
                print_warning(f"Failed: {rec.title} — {exc}")

        return results

    def _check_safety(self, rec: Recommendation) -> None:
        """Enforce safety rails for auto mode."""
        max_risk = self.context.config.automation.max_auto_risk_level
        risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        max_idx = next((i for i, r in enumerate(risk_order) if r.value == max_risk), 1)
        rec_idx = next((i for i, r in enumerate(risk_order) if r == rec.risk_level), 3)
        if rec_idx > max_idx:
            raise SafetyError(
                f"Action '{rec.title}' has risk level {rec.risk_level} which exceeds "
                f"max auto risk level '{max_risk}'"
            )

        # Check protected resources
        protected = set(self.context.config.automation.protected_clusters + self.context.config.automation.protected_jobs)
        if rec.resource_id in protected:
            raise SafetyError(f"Resource '{rec.resource_id}' is protected from auto actions")
