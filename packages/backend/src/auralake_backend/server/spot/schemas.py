from __future__ import annotations

from pydantic import BaseModel


class SpotApplyRequest(BaseModel):
    cluster_id: str | None = None
