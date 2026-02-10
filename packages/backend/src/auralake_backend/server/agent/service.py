from __future__ import annotations

from auralake_shared.core.context import ExecutionContext


class AgentService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def status(self) -> dict:
        from auralake_backend.db.agent_state_repository import AgentStateRepository

        repo = AgentStateRepository(self.context.config)
        state = repo.get_state()
        return state.model_dump() if state else {"status": "unknown"}

    def start(self) -> dict:
        # TODO: collector runs as a server background task; start is a stub
        return {"status": "started", "detail": "Collector agent start requested."}

    def stop(self) -> dict:
        # TODO: collector runs as a server background task; stop is a stub
        return {"status": "stopped", "detail": "Collector agent stop requested."}
