from __future__ import annotations

from pydantic import BaseModel, Field


class OptimizeRequest(BaseModel):
    table: str = Field(min_length=1, max_length=500)


class VacuumRequest(BaseModel):
    table: str = Field(min_length=1, max_length=500)
    retention_hours: int = Field(default=168, ge=0)
