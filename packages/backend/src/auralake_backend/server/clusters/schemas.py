from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ResizeRequest(BaseModel):
    workers: int | None = Field(default=None, ge=1, le=10000)
    instance_type: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def at_least_one_field(self) -> ResizeRequest:
        if self.workers is None and self.instance_type is None:
            raise ValueError("At least one of 'workers' or 'instance_type' must be provided")
        return self
