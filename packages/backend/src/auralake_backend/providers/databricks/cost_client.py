"""Databricks platform-level cost client using system billing tables."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.billing import CostBreakdown, CostRecord, PriceRecord
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractCostClient

from auralake_backend.providers.databricks.auth import get_workspace_client


class DatabricksCostClient(AbstractCostClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def get_usage(
        self, start: date, end: date, group_by: list[str] | None = None
    ) -> list[CostRecord]:
        """Query system.billing.usage for DBU consumption."""
        group_cols = ", ".join(group_by) if group_by else "sku_name"
        sql = f"""
            SELECT usage_date, {group_cols},
                   SUM(usage_quantity) AS dbu_usage,
                   SUM(usage_quantity * pricing.default.effective_list.default) AS cost_usd
            FROM system.billing.usage
            WHERE usage_date BETWEEN '{start}' AND '{end}'
            GROUP BY usage_date, {group_cols}
            ORDER BY usage_date
        """
        try:
            results = self._execute_sql(sql)
            return [
                CostRecord(
                    date=row.get("usage_date", start),
                    sku=row.get("sku_name"),
                    cluster_id=row.get("cluster_id"),
                    job_id=row.get("job_id"),
                    dbu_usage=float(row.get("dbu_usage", 0)),
                    cost_usd=Decimal(str(row.get("cost_usd", 0))),
                )
                for row in results
            ]
        except Exception as exc:
            raise APIError("databricks", f"Failed to query billing: {exc}") from exc

    def get_pricing(self) -> list[PriceRecord]:
        """Fetch current SKU pricing."""
        sql = (
            "SELECT sku_name, pricing.default.effective_list.default AS unit_price "
            "FROM system.billing.list_prices"
        )
        try:
            results = self._execute_sql(sql)
            return [
                PriceRecord(
                    sku=row["sku_name"],
                    unit_price_usd=Decimal(str(row["unit_price"])),
                )
                for row in results
            ]
        except Exception as exc:
            raise APIError("databricks", f"Failed to query pricing: {exc}") from exc

    def get_usage_since(self, since: date) -> list[CostRecord]:
        """Fetch billing records from ``since`` to today."""
        return self.get_usage(since, date.today(), group_by=["sku_name", "cluster_id", "job_id"])

    def get_cost_breakdown(self, start: date, end: date) -> CostBreakdown:
        records = self.get_usage(start, end, group_by=["sku_name", "cluster_id", "job_id"])
        by_sku: dict[str, Decimal] = {}
        by_cluster: dict[str, Decimal] = {}
        by_job: dict[str, Decimal] = {}
        total = Decimal("0")
        for r in records:
            total += r.cost_usd
            if r.sku:
                by_sku[r.sku] = by_sku.get(r.sku, Decimal("0")) + r.cost_usd
            if r.cluster_id:
                by_cluster[r.cluster_id] = by_cluster.get(r.cluster_id, Decimal("0")) + r.cost_usd
            if r.job_id:
                by_job[r.job_id] = by_job.get(r.job_id, Decimal("0")) + r.cost_usd
        return CostBreakdown(
            total_cost_usd=total,
            by_sku=by_sku,
            by_cluster=by_cluster,
            by_job=by_job,
            period_start=start,
            period_end=end,
        )

    def _execute_sql(self, sql: str) -> list[dict]:
        """Execute SQL via a workspace with a SQL warehouse configured."""
        for ws_name, ws_config in self._config.workspaces.items():
            if ws_config.sql_warehouse_id:
                client = get_workspace_client(self._config, ws_name)
                result = client.statement_execution.execute_statement(
                    warehouse_id=ws_config.sql_warehouse_id,
                    statement=sql,
                )
                if result.result and result.result.data_array:
                    columns = [col.name for col in (result.manifest.schema.columns or [])]
                    return [dict(zip(columns, row)) for row in result.result.data_array]
                return []
        raise APIError("databricks", "No workspace with sql_warehouse_id configured")
