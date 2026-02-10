from __future__ import annotations

from pydantic import BaseModel


class EnforceRequest(BaseModel):
    tag_key: str
    default_value: str
