"""Repository for agent collector state persistence."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from auralake_shared.models.config import AuraLakeConfig


class AgentState(BaseModel):
    status: str = "idle"
    last_run: str | None = None
    error: str | None = None


class AgentStateRepository:
    def __init__(self, config: AuraLakeConfig) -> None:
        self.config = config

    def get_state(self) -> AgentState | None:
        # TODO: persist agent state in DB
        return AgentState()
