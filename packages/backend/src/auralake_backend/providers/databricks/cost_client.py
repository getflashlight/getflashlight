"""Databricks platform-level cost client using system billing tables."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import structlog
from auralake_shared.core.exceptions import APIError
from auralake_shared.models.billing import CostBreakdown, CostRecord, PriceRecord
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractCostClient

from auralake_backend.providers.databricks.auth import get_warehouse_id, get_workspace_client

logger = structlog.get_logger(__name__)


class DatabricksCostClient(AbstractCostClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def get_usage(
        self, start: date, end: date, group_by: list[str] | None = None
    ) -> list[CostRecord]:
        """Query system.billing.usage for DBU consumption.

        Fetches raw DBU usage grouped by the requested columns, then
        multiplies by SKU prices fetched via ``_get_pricing_map()`` to
        compute cost_usd in Python (more resilient across Databricks tiers).
        """
        # Map group_by column names to correct SQL references
        col_map: dict[str, str] = {
            "sku_name": "sku_name",
            "cluster_id": "usage_metadata.cluster_id AS cluster_id",
            "job_id": "usage_metadata.job_id AS job_id",
        }
        raw_cols = group_by or ["sku_name"]
        select_cols = ", ".join(col_map.get(c, c) for c in raw_cols)
        # GROUP BY uses the alias names (without AS)
        group_aliases = ", ".join(c for c in raw_cols)

        sql = f"""
            SELECT usage_date, {select_cols},
                   SUM(usage_quantity) AS dbu_usage
            FROM system.billing.usage
            WHERE usage_date BETWEEN '{start}' AND '{end}'
            GROUP BY usage_date, {group_aliases}
            ORDER BY usage_date
        """
        try:
            results = self._execute_sql(sql)
            pricing = self._get_pricing_map()

            return [
                CostRecord(
                    date=row.get("usage_date", start),
                    sku=row.get("sku_name"),
                    cluster_id=row.get("cluster_id"),
                    job_id=row.get("job_id"),
                    dbu_usage=float(row.get("dbu_usage", 0)),
                    cost_usd=Decimal(
                        str(
                            float(row.get("dbu_usage", 0))
                            * float(pricing.get(row.get("sku_name", ""), 0))
                        )
                    ),
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

    def _get_pricing_map(self) -> dict[str, Decimal]:
        """Return {sku_name: unit_price_usd} for cost calculation."""
        try:
            prices = self.get_pricing()
            return {p.sku: p.unit_price_usd for p in prices}
        except Exception as exc:
            logger.warning("pricing_fetch_failed", error=str(exc))
            return {}

    def get_usage_since(self, since: date) -> list[CostRecord]:
        """Fetch billing records from ``since`` to today."""
        return self.get_usage(
            since,
            date.today(),
            group_by=["sku_name", "cluster_id", "job_id"],
        )

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
        """Execute SQL via the first workspace with a reachable warehouse."""
        for ws_name, ws_config in self._config.workspaces.items():
            try:
                client = get_workspace_client(self._config, ws_name)
                wh_id = get_warehouse_id(client, ws_config.sql_warehouse_id)
                result = client.statement_execution.execute_statement(
                    warehouse_id=wh_id,
                    statement=sql,
                )
                if result.result and result.result.data_array:
                    columns = [col.name for col in (result.manifest.schema.columns or [])]
                    return [dict(zip(columns, row)) for row in result.result.data_array]
                return []
            except Exception as exc:
                logger.warning(
                    "sql_workspace_failed",
                    workspace=ws_name,
                    error=str(exc),
                )
                continue
        raise APIError("databricks", "No workspace with a reachable SQL warehouse")
