from __future__ import annotations

from decimal import Decimal

from auralake_shared.core.context import ExecutionContext


class BudgetService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def list_budgets(self) -> list[dict]:
        # TODO: implement budget persistence and retrieval
        return []

    def create(self, name: str, amount: Decimal, scope: str = "workspace") -> dict:
        # TODO: implement budget creation
        return {"name": name, "amount": str(amount), "scope": scope, "status": "created"}

    def update(
        self,
        budget_id: str,
        name: str | None = None,
        amount: Decimal | None = None,
        scope: str | None = None,
    ) -> dict:
        # TODO: implement budget update
        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = name
        if amount is not None:
            changes["amount"] = str(amount)
        if scope is not None:
            changes["scope"] = scope
        return {"budget_id": budget_id, "changes": changes, "status": "updated"}

    def alerts(self) -> list[dict]:
        # TODO: implement budget alert retrieval
        return []
