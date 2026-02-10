from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CleanupParams(BaseModel):
    resource_type: Literal["cluster", "endpoint", "warehouse", "all"] | None = None
