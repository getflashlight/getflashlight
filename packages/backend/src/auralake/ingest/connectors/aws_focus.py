"""AWS native FOCUS export connector.

AWS Data Exports emit FOCUS 1.0 Parquet to S3. Because the export is already
FOCUS, this connector is mostly a column rename plus light type coercion — the
preferred path per the "native exports where available" decision. EC2/EBS/S3
lines (the infra backing Databricks classic compute) are included here, which is
exactly what the TCO join needs.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import boto3
import pyarrow.parquet as pq

from auralake.core.exceptions import ConnectorError
from auralake.core.logging import get_logger
from auralake.focus.enums import ProviderName
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import AwsFocusConfig, env
from auralake.ingest.connectors._coerce import (
    to_charge_category,
    to_datetime,
    to_decimal,
    to_service_category,
)

logger = get_logger(__name__)


class AwsFocusConnector(Connector):
    name = "aws_focus"

    def __init__(self, config: AwsFocusConfig) -> None:
        self._config = config
        self._s3 = boto3.client(
            "s3",
            region_name=config.region,
            aws_access_key_id=env(config.access_key_env),
            aws_secret_access_key=env(config.secret_key_env),
        )

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        keys = self._list_parquet_keys()
        if not keys:
            logger.warning("aws_focus_no_objects", prefix=self._config.s3_prefix)
            return
        for key in keys:
            yield from self._read_object(key, window)

    def _list_parquet_keys(self) -> list[str]:
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(
                Bucket=self._config.s3_bucket, Prefix=self._config.s3_prefix
            ):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(".parquet"):
                        keys.append(obj["Key"])
            return keys
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"S3 list failed: {exc}") from exc

    def _read_object(self, key: str, window: IngestWindow) -> Iterator[FocusRecord]:
        try:
            body = self._s3.get_object(Bucket=self._config.s3_bucket, Key=key)["Body"].read()
            table = pq.read_table(io.BytesIO(body))  # type: ignore[no-untyped-call]
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"Parquet read failed for {key}: {exc}") from exc

        for row in table.to_pylist():
            record = self._map_row(row)
            if record is None:
                continue
            if not (window.start <= record.billing_period_start <= window.end) and not (
                window.start <= record.charge_period_start.date() <= window.end
            ):
                continue
            yield record

    def _map_row(self, row: dict[str, Any]) -> FocusRecord | None:
        charge_start = row.get("ChargePeriodStart")
        if charge_start is None:
            return None
        return FocusRecord(
            provider_name=ProviderName.AWS,
            billing_account_id=str(row.get("BillingAccountId", "unknown")),
            billing_account_name=row.get("BillingAccountName"),
            sub_account_id=_opt_str(row.get("SubAccountId")),
            sub_account_name=row.get("SubAccountName"),
            billing_period_start=to_datetime(row.get("BillingPeriodStart", charge_start)).date(),
            billing_period_end=to_datetime(
                row.get("BillingPeriodEnd", row.get("ChargePeriodEnd", charge_start))
            ).date(),
            charge_period_start=to_datetime(charge_start),
            charge_period_end=to_datetime(row.get("ChargePeriodEnd", charge_start)),
            billing_currency=str(row.get("BillingCurrency", "USD")),
            billed_cost=to_decimal(row.get("BilledCost")),
            effective_cost=to_decimal(row.get("EffectiveCost")),
            list_cost=to_decimal(row.get("ListCost")),
            contracted_cost=to_decimal(row.get("ContractedCost")),
            charge_category=to_charge_category(row.get("ChargeCategory")),
            charge_description=row.get("ChargeDescription"),
            service_category=to_service_category(row.get("ServiceCategory")),
            service_name=str(row.get("ServiceName", "Unknown")),
            sku_id=_opt_str(row.get("SkuId")),
            region_id=_opt_str(row.get("RegionId")),
            resource_id=_opt_str(row.get("ResourceId")),
            resource_name=_opt_str(row.get("ResourceName")),
            resource_type=_opt_str(row.get("ResourceType")),
            consumed_quantity=_opt_float(row.get("ConsumedQuantity")),
            consumed_unit=_opt_str(row.get("ConsumedUnit")),
            tags=_parse_tags(row.get("Tags")),
            x_source_connector=self.name,
        )


def _opt_str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _opt_float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_tags(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            return {}
    return {}
