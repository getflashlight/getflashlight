from __future__ import annotations

from pydantic import BaseModel


class CompareParams(BaseModel):
    target_provider: str
