from __future__ import annotations

from pydantic import BaseModel


class ResizeRequest(BaseModel):
    workers: int | None = None
    instance_type: str | None = None
