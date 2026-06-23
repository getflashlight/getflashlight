"""Minimal HTTP client for the Auralake backend."""

from __future__ import annotations

import os
from typing import Any

import httpx


class Client:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("AURALAKE_API_URL", "http://localhost:8001")).rstrip("/")
        self.api_key = api_key or os.environ.get("AURALAKE_API_KEY")

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = httpx.get(
            f"{self.base_url}{path}", params=params, headers=self._headers(), timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        resp = httpx.post(
            f"{self.base_url}{path}", json=json or {}, headers=self._headers(), timeout=600
        )
        resp.raise_for_status()
        return resp.json()
