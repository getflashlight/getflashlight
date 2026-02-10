from __future__ import annotations

from pydantic import BaseModel


class BreakdownParams(BaseModel):
    days: int = 30
    by: str = "sku"
