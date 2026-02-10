from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.config import BudgetScope
from pydantic import BaseModel, Field


class CreateBudgetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    scope: BudgetScope = BudgetScope.WORKSPACE


class UpdateBudgetRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    scope: BudgetScope | None = None
