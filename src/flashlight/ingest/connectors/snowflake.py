"""Snowflake connector — reads ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY.

Maps Snowflake's daily cost-in-currency view to canonical FOCUS records. Each row
is one (account, service_type, usage_type, date) grain — already in currency, so no
credit-to-dollar conversion is needed here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from flashlight.core.exceptions import ConnectorError
from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.focus import sql_mapping
from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import Connector, IngestWindow, ProgressCallback
from flashlight.ingest.config import SnowflakeConfig, effective_connector_name, env
from flashlight.ingest.connectors._snowflake_supported_drivers import check_support_status
from flashlight.lake import bronze
from flashlight.lake.driver_health_schema import DriverHealthRecord

logger = get_logger(__name__)

_QUERY = """\
SELECT
    ORGANIZATION_NAME,
    CONTRACT_NUMBER,
    ACCOUNT_NAME,
    ACCOUNT_LOCATOR,
    REGION,
    SERVICE_LEVEL,
    USAGE_DATE,
    USAGE_TYPE,
    USAGE,
    CURRENCY,
    USAGE_IN_CURRENCY,
    BALANCE_SOURCE,
    BILLING_TYPE,
    RATING_TYPE,
    SERVICE_TYPE,
    IS_ADJUSTMENT
FROM {database}.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE USAGE_DATE BETWEEN %(start)s AND %(end)s
"""

# The cost source is already at a sensible financial grain: one daily
# (organization/account/service/usage-type) fact.  Filter and shape it in Snowflake,
# then let the shared DuckDB mapping only enforce the canonical FOCUS schema and write
# Parquet in bulk.  Quoted aliases retain the FOCUS casing through pandas and DuckDB.
_BULK_QUERY = """\
SELECT
    'Snowflake' AS "ProviderName",
    ORGANIZATION_NAME AS "BillingAccountId",
    ACCOUNT_NAME AS "SubAccountId",
    ACCOUNT_LOCATOR AS "SubAccountName",
    DATE_TRUNC('MONTH', USAGE_DATE)::DATE AS "BillingPeriodStart",
    DATEADD(MONTH, 1, DATE_TRUNC('MONTH', USAGE_DATE))::DATE AS "BillingPeriodEnd",
    USAGE_DATE::TIMESTAMP_NTZ AS "ChargePeriodStart",
    DATEADD(DAY, 1, USAGE_DATE)::TIMESTAMP_NTZ AS "ChargePeriodEnd",
    CURRENCY AS "BillingCurrency",
    USAGE_IN_CURRENCY AS "BilledCost",
    USAGE_IN_CURRENCY AS "EffectiveCost",
    USAGE_IN_CURRENCY AS "ListCost",
    USAGE_IN_CURRENCY AS "ContractedCost",
    IFF(IS_ADJUSTMENT, 'Adjustment', 'Usage') AS "ChargeCategory",
    USAGE_TYPE AS "ChargeDescription",
    CASE UPPER(SERVICE_TYPE)
        WHEN 'WAREHOUSE_METERING' THEN 'Compute'
        WHEN 'CLOUD_SERVICES' THEN 'Compute'
        WHEN 'QUERY_ACCELERATION' THEN 'Compute'
        WHEN 'SERVERLESS_COMPUTE' THEN 'Compute'
        WHEN 'STORAGE' THEN 'Storage'
        WHEN 'DATA_TRANSFER' THEN 'Networking'
        WHEN 'REPLICATION' THEN 'Networking'
        WHEN 'AI_SERVICES' THEN 'AI and Machine Learning'
        ELSE 'Other'
    END AS "ServiceCategory",
    SERVICE_TYPE AS "ServiceName",
    USAGE_TYPE AS "SkuId",
    REGION AS "RegionId",
    USAGE AS "ConsumedQuantity",
    'Credits' AS "ConsumedUnit"
FROM {database}.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE USAGE_DATE BETWEEN %(start)s AND %(end)s
  AND USAGE_IN_CURRENCY IS NOT NULL
"""

_DRIVER_HEALTH_SQL = (Path(__file__).parent / "sql" / "snowflake_driver_health.sql").read_text()

_SERVICE_TYPE_CATEGORY: dict[str, ServiceCategory] = {
    "WAREHOUSE_METERING": ServiceCategory.COMPUTE,
    "CLOUD_SERVICES": ServiceCategory.COMPUTE,
    "QUERY_ACCELERATION": ServiceCategory.COMPUTE,
    "SERVERLESS_COMPUTE": ServiceCategory.COMPUTE,
    "STORAGE": ServiceCategory.STORAGE,
    "DATA_TRANSFER": ServiceCategory.NETWORKING,
    "REPLICATION": ServiceCategory.NETWORKING,
    "AI_SERVICES": ServiceCategory.AI_AND_MACHINE_LEARNING,
}


def _service_category(service_type: str | None) -> ServiceCategory:
    if not service_type:
        return ServiceCategory.OTHER
    return _SERVICE_TYPE_CATEGORY.get(service_type.upper(), ServiceCategory.OTHER)


def _billing_period(usage_date: date) -> tuple[date, date]:
    """Return (first-of-month, first-of-next-month) for the given date."""
    start = usage_date.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


class SnowflakeConnector(Connector):
    """Pull Snowflake cost from ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY."""

    name = "snowflake"

    def __init__(self, config: SnowflakeConfig) -> None:
        self._config = config
        self.name = effective_connector_name(config)

    def _connect(self) -> snowflake.connector.SnowflakeConnection:
        user = env(self._config.user_env)
        if not user:
            raise ConnectorError(self.name, f"Missing user env {self._config.user_env}")
        params: dict[str, object] = {
            "account": self._config.account,
            "user": user,
            "role": self._config.role,
            "database": self._config.database,
            "schema": "ORGANIZATION_USAGE",
        }
        if self._config.private_key_path:
            key_bytes = Path(self._config.private_key_path).read_bytes()
            private_key = serialization.load_pem_private_key(
                key_bytes, password=None, backend=default_backend()
            )
            pkb = private_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            params["private_key"] = pkb
        elif self._config.authenticator:
            params["authenticator"] = self._config.authenticator
        else:
            password = env(self._config.password_env)
            if not password:
                raise ConnectorError(
                    self.name, f"Missing password env {self._config.password_env}"
                )
            params["password"] = password
        if self._config.warehouse:
            params["warehouse"] = self._config.warehouse
        return snowflake.connector.connect(**params)

    def ingest(
        self,
        window: IngestWindow,
        *,
        run_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Bulk-ingest Snowflake cost rows without creating a Python model per row.

        Snowflake applies the date/null filters and projects rows into FOCUS-shaped
        columns.  The connector fetches one tabular batch, after which DuckDB runs the
        shared canonical mapping and writes the complete BRONZE window in one COPY.
        """
        query = _BULK_QUERY.format(database=self._config.database)
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(query, {"start": window.start, "end": window.end})
                frame = cur.fetch_pandas_all()
            finally:
                cur.close()
        finally:
            conn.close()

        con = duckdb.connect()
        try:
            sql_mapping.ensure_helpers(con)
            con.register("_snowflake_source", frame)
            source_sql = "_snowflake_source"
            mapped = sql_mapping.mapping_sql(
                source_sql,
                connector=self.name,
                run_id=run_id,
                present=sql_mapping.present_columns(con, source_sql),
            )
            return bronze.write_window_sql(
                self.name,
                window,
                con,
                mapped,
                base_currency=get_settings().base_currency,
            )
        except Exception as exc:
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(self.name, f"Bulk cost ingest failed: {exc}") from exc
        finally:
            con.close()

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        query = _QUERY.format(database=self._config.database)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(query, {"start": window.start, "end": window.end})
            columns = [desc[0] for desc in cur.description] if cur.description else []
            for raw_row in cur:
                row = dict(zip(columns, raw_row, strict=False))
                record = self._map_row(row)
                if record is not None:
                    yield record
            cur.close()
        finally:
            conn.close()

    def _map_row(self, row: dict[str, Any]) -> FocusRecord | None:
        usage_date = row["USAGE_DATE"]
        if usage_date is None:
            return None
        if isinstance(usage_date, str):
            usage_date = date.fromisoformat(usage_date.strip('"'))
        elif isinstance(usage_date, datetime):
            usage_date = usage_date.date()

        bp_start, bp_end = _billing_period(usage_date)
        charge_start = datetime(usage_date.year, usage_date.month, usage_date.day)
        charge_end = charge_start + timedelta(days=1)

        cost = Decimal(str(row.get("USAGE_IN_CURRENCY") or 0))
        is_adjustment = row.get("IS_ADJUSTMENT")
        charge_category = (
            ChargeCategory.ADJUSTMENT if is_adjustment else ChargeCategory.USAGE
        )

        service_type = row.get("SERVICE_TYPE") or "OTHER"
        usage_type = row.get("USAGE_TYPE") or ""

        return FocusRecord(
            provider_name=ProviderName.SNOWFLAKE,
            billing_account_id=row.get("ORGANIZATION_NAME") or "unknown",
            sub_account_id=row.get("ACCOUNT_NAME"),
            sub_account_name=row.get("ACCOUNT_LOCATOR"),
            billing_period_start=bp_start,
            billing_period_end=bp_end,
            charge_period_start=charge_start,
            charge_period_end=charge_end,
            billing_currency=row.get("CURRENCY") or "USD",
            billed_cost=cost,
            effective_cost=cost,
            list_cost=cost,
            contracted_cost=cost,
            charge_category=charge_category,
            charge_description=usage_type,
            service_category=_service_category(service_type),
            service_name=service_type,
            sku_id=usage_type,
            region_id=row.get("REGION"),
            consumed_quantity=float(row.get("USAGE") or 0),
            consumed_unit="Credits",
            x_source_connector=self.name,
        )

    def fetch_driver_health(self, window: IngestWindow) -> Iterator[DriverHealthRecord]:
        """Query ACCOUNT_USAGE.SESSIONS + QUERY_HISTORY for client driver fleet health."""
        query = _DRIVER_HEALTH_SQL.format(database=self._config.database)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(query, {"start": window.start, "end": window.end})
            columns = [desc[0] for desc in cur.description] if cur.description else []
            for raw_row in cur:
                row = dict(zip(columns, raw_row, strict=False))
                yield self._map_driver_health_row(row)
            cur.close()
        finally:
            conn.close()

    def _map_driver_health_row(self, row: dict[str, Any]) -> DriverHealthRecord:
        charge_month = row["CHARGE_MONTH"]
        if isinstance(charge_month, datetime):
            charge_month = charge_month.date()
        elif isinstance(charge_month, str):
            charge_month = date.fromisoformat(charge_month[:10])

        client_driver = row.get("CLIENT_DRIVER")
        return DriverHealthRecord(
            provider_name="Snowflake",
            charge_month=charge_month,
            client_driver=client_driver,
            client_application=row.get("CLIENT_APPLICATION"),
            executed_by=row.get("EXECUTED_BY"),
            query_count=int(row.get("QUERY_COUNT") or 0),
            support_status=check_support_status(client_driver),
            x_source_connector=self.name,
        )
