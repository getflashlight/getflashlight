from __future__ import annotations

from pydantic import BaseModel, Field


class SpotApplyRequest(BaseModel):
    cluster_id: str | None = Field(default=None, min_length=1, max_length=200)
