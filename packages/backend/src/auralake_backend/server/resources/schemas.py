from __future__ import annotations

from pydantic import BaseModel


class CleanupParams(BaseModel):
    resource_type: str | None = None
