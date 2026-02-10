from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class CreateBudgetRequest(BaseModel):
    name: str
    amount: Decimal
    scope: str = "workspace"


class UpdateBudgetRequest(BaseModel):
    name: str | None = None
    amount: Decimal | None = None
    scope: str | None = None
