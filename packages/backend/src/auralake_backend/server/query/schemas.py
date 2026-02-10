from __future__ import annotations

from pydantic import BaseModel


class ExpensiveParams(BaseModel):
    days: int = 30
    top_n: int = 20


class PlansParams(BaseModel):
    workspace: str | None = None
