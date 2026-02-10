from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .schemas import CreateBudgetRequest, UpdateBudgetRequest
from .service import BudgetService

router = APIRouter()


@router.get("/list")
def list_budgets(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[dict]:
    """List all configured budgets."""
    return BudgetService(context).list_budgets()


@router.get("/alerts")
def alerts(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[dict]:
    """List active budget alerts."""
    return BudgetService(context).alerts()


@router.post("/create")
def create(
    body: CreateBudgetRequest,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> dict:
    """Create a new budget."""
    return BudgetService(context).create(
        name=body.name,
        amount=body.amount,
        scope=body.scope,
    )


@router.put("/{budget_id}")
def update(
    budget_id: str,
    body: UpdateBudgetRequest,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> dict:
    """Update an existing budget."""
    return BudgetService(context).update(
        budget_id=budget_id,
        name=body.name,
        amount=body.amount,
        scope=body.scope,
    )
