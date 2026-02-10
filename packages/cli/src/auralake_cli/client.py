"""HTTP client for the Auralake server API.

Provides typed methods that call the REST API and return Pydantic models.
This is the CLI's sole interface to the server — no direct imports of
analyzers, actions, providers, or DB code.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx


class AuthenticationError(Exception):
    """Raised when the server returns 401."""


def _handle_response(resp: httpx.Response) -> None:
    """Check response status, raising a friendly error on 401."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            print(
                "Error: Authentication required. Run `auralake login` or set AURALAKE_API_KEY.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        raise


class AuralakeClient:
    """Thin HTTP client wrapping the Auralake server API."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url,
            timeout=60.0,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        resp = self._client.get(path, params=params)
        _handle_response(resp)
        return resp.json()

    def _post(self, path: str, body: dict[str, Any] | None = None, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        resp = self._client.post(path, json=body, params=params)
        _handle_response(resp)
        return resp.json()

    def _put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = self._client.put(path, json=body)
        _handle_response(resp)
        return resp.json()

    def _delete(self, path: str) -> None:
        resp = self._client.delete(path)
        _handle_response(resp)

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------

    def agent_status(self) -> dict:
        return self._get("/api/v1/agent/status")

    def agent_status_connection(self, connection_id: str) -> dict:
        return self._get(f"/api/v1/agent/status/{connection_id}")

    def agent_collect(self, connection_id: str) -> dict:
        return self._post(f"/api/v1/agent/collect/{connection_id}")

    def agent_retry(self, connection_id: str, worker_name: str) -> dict:
        return self._post(f"/api/v1/agent/retry/{connection_id}/{worker_name}")

    def agent_cancel(self, connection_id: str) -> dict:
        return self._post(f"/api/v1/agent/cancel/{connection_id}")

    def agent_history(self) -> list[dict]:
        return self._get("/api/v1/agent/history")

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def connections_list(self) -> list[dict]:
        return self._get("/api/v1/connections")

    def connections_get(self, connection_id: str) -> dict:
        return self._get(f"/api/v1/connections/{connection_id}")

    def connections_create(
        self,
        provider: str,
        name: str,
        is_default: bool = False,
        config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "provider": provider,
            "name": name,
            "is_default": is_default,
            "config": config or {},
        }
        if credentials is not None:
            body["credentials"] = credentials
        return self._post("/api/v1/connections", body)

    def connections_update(
        self,
        connection_id: str,
        is_default: bool | None = None,
        config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if is_default is not None:
            body["is_default"] = is_default
        if config is not None:
            body["config"] = config
        if credentials is not None:
            body["credentials"] = credentials
        return self._put(f"/api/v1/connections/{connection_id}", body)

    def connections_delete(self, connection_id: str) -> None:
        self._delete(f"/api/v1/connections/{connection_id}")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return self._get("/health")
