from __future__ import annotations

from pydantic import BaseModel


class OptimizeRequest(BaseModel):
    table: str


class VacuumRequest(BaseModel):
    table: str
    retention_hours: int = 168
