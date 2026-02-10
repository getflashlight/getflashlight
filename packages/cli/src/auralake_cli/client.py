"""HTTP client for the Auralake server API.

Provides typed methods that call the REST API and return Pydantic models.
This is the CLI's sole interface to the server — no direct imports of
analyzers, actions, providers, or DB code.
"""

from __future__ import annotations

from typing import Any

import httpx
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


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
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict[str, Any] | None = None, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        resp = self._client.post(path, json=body, params=params)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = self._client.put(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def _analysis(self, path: str, **params: Any) -> AnalysisResult:
        data = self._get(path, **params)
        return AnalysisResult.model_validate(data)

    def _action(
        self, path: str, body: dict[str, Any] | None = None, **params: Any
    ) -> list[ActionResult]:
        data = self._post(path, body, **params)
        if isinstance(data, list):
            return [ActionResult.model_validate(d) for d in data]
        return [ActionResult.model_validate(data)]

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    def cost_report(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/cost/report", workspace=workspace)

    def cost_breakdown(self, days: int = 30, by: str = "sku", workspace: str | None = None) -> dict:
        return self._get("/api/v1/cost/breakdown", days=days, by=by, workspace=workspace)

    def cost_tco(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/cost/tco", workspace=workspace)

    def cost_infra(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/cost/infra", workspace=workspace)

    # ------------------------------------------------------------------
    # Clusters
    # ------------------------------------------------------------------

    def clusters_analyze(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/clusters/analyze", workspace=workspace)

    def clusters_list(self, workspace: str | None = None) -> list[dict]:
        return self._get("/api/v1/clusters/list", workspace=workspace)

    def clusters_get(self, cluster_id: str, workspace: str | None = None) -> dict:
        return self._get(f"/api/v1/clusters/{cluster_id}", workspace=workspace)

    def clusters_resize(
        self,
        cluster_id: str,
        workers: int | None = None,
        instance_type: str | None = None,
        workspace: str | None = None,
    ) -> ActionResult:
        body = {}
        if workers is not None:
            body["workers"] = workers
        if instance_type is not None:
            body["instance_type"] = instance_type
        results = self._action(f"/api/v1/clusters/{cluster_id}/resize", body, workspace=workspace)
        return results[0]

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    def resources_scan(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/resources/scan", workspace=workspace)

    def resources_report(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/resources/report", workspace=workspace)

    def resources_cleanup(
        self, resource_type: str | None = None, workspace: str | None = None
    ) -> list[ActionResult]:
        return self._action(
            "/api/v1/resources/cleanup",
            {"resource_type": resource_type} if resource_type else None,
            workspace=workspace,
        )

    # ------------------------------------------------------------------
    # Spot
    # ------------------------------------------------------------------

    def spot_analyze(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/spot/analyze", workspace=workspace)

    def spot_recommend(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/spot/recommend", workspace=workspace)

    def spot_apply(
        self, cluster_id: str | None = None, workspace: str | None = None
    ) -> list[ActionResult]:
        return self._action(
            "/api/v1/spot/apply",
            {"cluster_id": cluster_id} if cluster_id else None,
            workspace=workspace,
        )

    # ------------------------------------------------------------------
    # Delta
    # ------------------------------------------------------------------

    def delta_scan(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/delta/scan", workspace=workspace)

    def delta_optimize(self, table: str, workspace: str | None = None) -> ActionResult:
        results = self._action("/api/v1/delta/optimize", {"table": table}, workspace=workspace)
        return results[0]

    def delta_vacuum(
        self,
        table: str,
        retention_hours: int = 168,
        workspace: str | None = None,
    ) -> ActionResult:
        results = self._action(
            "/api/v1/delta/vacuum",
            {"table": table, "retention_hours": retention_hours},
            workspace=workspace,
        )
        return results[0]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def jobs_analyze(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/jobs/analyze", workspace=workspace)

    def jobs_stale(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/jobs/stale", workspace=workspace)

    def jobs_recommend(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/jobs/recommend", workspace=workspace)

    def jobs_consolidate(self, workspace: str | None = None) -> list[ActionResult]:
        return self._action("/api/v1/jobs/consolidate", workspace=workspace)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query_analyze(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/query/analyze", workspace=workspace)

    def query_expensive(
        self, days: int = 7, top_n: int = 10, workspace: str | None = None
    ) -> list[dict]:
        return self._get("/api/v1/query/expensive", days=days, top_n=top_n, workspace=workspace)

    def query_plans(self, workspace: str | None = None) -> list[dict]:
        return self._get("/api/v1/query/plans", workspace=workspace)

    # ------------------------------------------------------------------
    # Policies
    # ------------------------------------------------------------------

    def policies_audit(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/policies/audit", workspace=workspace)

    def policies_recommend(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/policies/recommend", workspace=workspace)

    def policies_apply(self, workspace: str | None = None) -> list[ActionResult]:
        return self._action("/api/v1/policies/apply", workspace=workspace)

    # ------------------------------------------------------------------
    # Budgets
    # ------------------------------------------------------------------

    def budgets_list(self, workspace: str | None = None) -> list[dict]:
        return self._get("/api/v1/budgets/list", workspace=workspace)

    def budgets_create(self, name: str, amount: float, scope: str | None = None) -> dict:
        return self._post(
            "/api/v1/budgets/create",
            {"name": name, "amount": amount, "scope": scope},
        )

    def budgets_update(
        self,
        budget_id: str,
        name: str | None = None,
        amount: float | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if amount is not None:
            body["amount"] = amount
        return self._put(f"/api/v1/budgets/{budget_id}", body)

    def budgets_alerts(self, workspace: str | None = None) -> list[dict]:
        return self._get("/api/v1/budgets/alerts", workspace=workspace)

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def tags_scan(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/tags/scan", workspace=workspace)

    def tags_report(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/tags/report", workspace=workspace)

    def tags_enforce(self, workspace: str | None = None) -> list[ActionResult]:
        return self._action("/api/v1/tags/enforce", workspace=workspace)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def routing_analyze(self, workspace: str | None = None) -> AnalysisResult:
        return self._analysis("/api/v1/routing/analyze", workspace=workspace)

    def routing_compare(
        self, target_provider: str | None = None, workspace: str | None = None
    ) -> dict:
        return self._get(
            "/api/v1/routing/compare",
            target_provider=target_provider,
            workspace=workspace,
        )

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------

    def agent_status(self) -> dict:
        return self._get("/api/v1/agent/status")

    def agent_start(self) -> dict:
        return self._post("/api/v1/agent/start")

    def agent_stop(self) -> dict:
        return self._post("/api/v1/agent/stop")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return self._get("/health")
