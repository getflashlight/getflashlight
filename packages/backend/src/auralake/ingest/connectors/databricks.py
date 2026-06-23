"""Databricks connector: system.billing.usage → FOCUS.

Databricks system tables are not FOCUS-native, so we map them. Two facts drive
the mapping:

* Cost isn't in ``system.billing.usage`` — it's ``usage_quantity`` (DBUs) times
  the SKU list price from ``system.billing.list_prices``.
* The compute class (classic vs serverless) is stamped onto every row so the
  SILVER TCO layer knows whether to add separate AWS infra (classic) or not
  (serverless is billed all-in). This is the double-count guard's input.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

from databricks.sdk import WorkspaceClient

from auralake.core.exceptions import ConnectorError
from auralake.core.logging import get_logger
from auralake.focus.enums import (
    ChargeCategory,
    ComputeClass,
    ProviderName,
    ServiceCategory,
)
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import DatabricksConfig, env
from auralake.ingest.connectors._coerce import to_datetime

logger = get_logger(__name__)

# Map Databricks billing_origin_product → FOCUS ServiceCategory.
_PRODUCT_CATEGORY = {
    "JOBS": ServiceCategory.ANALYTICS,
    "SQL": ServiceCategory.ANALYTICS,
    "ALL_PURPOSE": ServiceCategory.ANALYTICS,
    "DLT": ServiceCategory.ANALYTICS,
    "MODEL_SERVING": ServiceCategory.AI_AND_MACHINE_LEARNING,
    "VECTOR_SEARCH": ServiceCategory.AI_AND_MACHINE_LEARNING,
    "MODEL_TRAINING": ServiceCategory.AI_AND_MACHINE_LEARNING,
}


class DatabricksConnector(Connector):
    name = "databricks"

    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        token = env(config.token_env)
        if not token:
            raise ConnectorError(self.name, f"Missing token env {config.token_env}")
        self._client = WorkspaceClient(host=config.host, token=token)

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        prices = self._price_map()
        rows = self._query_usage(window)
        for row in rows:
            record = self._map_row(row, prices)
            if record is not None:
                yield record

    # ── SQL execution ──────────────────────────────────────────────────────
    def _warehouse_id(self) -> str:
        if self._config.sql_warehouse_id:
            return self._config.sql_warehouse_id
        for wh in self._client.warehouses.list():
            if wh.id:
                return wh.id
        raise ConnectorError(self.name, "No SQL warehouse available")

    def _execute(self, sql: str) -> list[dict[str, Any]]:
        try:
            result = self._client.statement_execution.execute_statement(
                warehouse_id=self._warehouse_id(), statement=sql, wait_timeout="50s"
            )
            manifest = result.manifest
            if not (result.result and result.result.data_array and manifest and manifest.schema):
                return []
            cols = [c.name or "" for c in (manifest.schema.columns or [])]
            return [dict(zip(cols, r, strict=False)) for r in result.result.data_array]
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"SQL failed: {exc}") from exc

    def _price_map(self) -> dict[str, Decimal]:
        sql = (
            "SELECT sku_name, pricing.default.effective_list.default AS price "
            "FROM system.billing.list_prices"
        )
        out: dict[str, Decimal] = {}
        for row in self._execute(sql):
            try:
                out[row["sku_name"]] = Decimal(str(row.get("price") or "0"))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _query_usage(self, window: IngestWindow) -> list[dict[str, Any]]:
        sql = f"""
            SELECT usage_date, account_id, workspace_id, sku_name,
                   billing_origin_product, usage_quantity, usage_unit,
                   usage_start_time, usage_end_time,
                   usage_metadata.cluster_id AS cluster_id,
                   usage_metadata.warehouse_id AS warehouse_id,
                   usage_metadata.job_id AS job_id,
                   custom_tags
            FROM system.billing.usage
            WHERE usage_date BETWEEN '{window.start}' AND '{window.end}'
        """
        return self._execute(sql)

    # ── Mapping ────────────────────────────────────────────────────────────
    def _map_row(
        self, row: dict[str, Any], prices: dict[str, Decimal]
    ) -> FocusRecord | None:
        usage_date = row.get("usage_date")
        if usage_date is None:
            return None
        d = usage_date if isinstance(usage_date, date) else to_datetime(usage_date).date()

        sku = row.get("sku_name") or ""
        qty = float(row.get("usage_quantity") or 0)
        unit_price = prices.get(sku, Decimal("0"))
        cost = unit_price * Decimal(str(qty))

        compute_class = (
            ComputeClass.SERVERLESS if "SERVERLESS" in sku.upper() else ComputeClass.CLASSIC
        )
        product = str(row.get("billing_origin_product") or "")
        resource_id = row.get("cluster_id") or row.get("warehouse_id") or row.get("job_id")

        return FocusRecord(
            provider_name=ProviderName.DATABRICKS,
            billing_account_id=str(row.get("account_id", "unknown")),
            sub_account_id=_opt_str(row.get("workspace_id")),
            billing_period_start=d.replace(day=1),
            billing_period_end=d,
            charge_period_start=to_datetime(row.get("usage_start_time") or d),
            charge_period_end=to_datetime(row.get("usage_end_time") or d),
            billed_cost=cost,
            effective_cost=cost,
            list_cost=cost,
            contracted_cost=cost,
            charge_category=ChargeCategory.USAGE,
            charge_description=f"Databricks {product} ({sku})",
            service_category=_PRODUCT_CATEGORY.get(product, ServiceCategory.ANALYTICS),
            service_name=f"Databricks {product}".strip(),
            sku_id=sku or None,
            resource_id=_opt_str(resource_id),
            consumed_quantity=qty,
            consumed_unit=str(row.get("usage_unit") or "DBU"),
            tags=_parse_tags(row.get("custom_tags")),
            x_compute_class=compute_class,
            x_source_connector=self.name,
        )


def _opt_str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_tags(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}
