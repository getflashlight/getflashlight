"""Databricks cluster policy client."""

from __future__ import annotations

from typing import Any

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import DatabricksConfig

from auralake_backend.providers.databricks.auth import get_workspace_client


class DatabricksPolicyClient:
    """Wrapper around Databricks cluster policies API."""

    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def list_policies(self) -> list[dict[str, Any]]:
        try:
            policies = self._client.cluster_policies.list()
            return [
                {
                    "policy_id": p.policy_id,
                    "name": p.name,
                    "definition": p.definition,
                    "description": getattr(p, "description", ""),
                }
                for p in policies
            ]
        except Exception as exc:
            raise APIError("databricks", f"Failed to list policies: {exc}") from exc

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        try:
            p = self._client.cluster_policies.get(policy_id)
            return {
                "policy_id": p.policy_id,
                "name": p.name,
                "definition": p.definition,
            }
        except Exception as exc:
            raise APIError("databricks", f"Failed to get policy {policy_id}: {exc}") from exc
