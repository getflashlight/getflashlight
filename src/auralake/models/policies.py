from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PolicyDefinition(BaseModel):
    policy_id: str | None = None
    name: str
    definition: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    max_dbus_per_hour: float | None = None
    allowed_instance_types: list[str] = Field(default_factory=list)


class TagViolation(BaseModel):
    resource_type: str
    resource_id: str
    resource_name: str
    missing_tags: list[str] = Field(default_factory=list)


class TagPolicy(BaseModel):
    required_tags: list[str] = Field(default_factory=list)
    violations: list[TagViolation] = Field(default_factory=list)
