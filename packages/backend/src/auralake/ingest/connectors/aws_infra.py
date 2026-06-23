"""AWS infra connector (Cost Explorer) — fallback when no native FOCUS export.

Groups EC2/EBS/S3 spend by the Databricks cluster tag so the SILVER TCO layer
can attach it to classic-compute DBU rows. Prefer ``aws_focus`` when available;
this exists for accounts without Data Exports configured. Disabled by default.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import boto3

from auralake.core.exceptions import ConnectorError
from auralake.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import AwsInfraConfig, env
from auralake.ingest.connectors._coerce import to_decimal

# AWS service string → (FOCUS ServiceCategory, normalized service name).
_SERVICE_MAP = {
    "Amazon Elastic Compute Cloud - Compute": (ServiceCategory.COMPUTE, "AmazonEC2"),
    "Amazon Elastic Block Store": (ServiceCategory.STORAGE, "AmazonEBS"),
    "Amazon Simple Storage Service": (ServiceCategory.STORAGE, "AmazonS3"),
}


class AwsInfraConnector(Connector):
    name = "aws_infra"

    def __init__(self, config: AwsInfraConfig) -> None:
        self._config = config
        self._ce = boto3.client(
            "ce",
            region_name=config.region,
            aws_access_key_id=env(config.access_key_env),
            aws_secret_access_key=env(config.secret_key_env),
        )

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        try:
            # CE end date is exclusive; extend by one day to include `window.end`.
            results = self._paginate(
                TimePeriod={"Start": str(window.start), "End": str(_next_day(window.end))},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                Filter=self._build_filter(),
                GroupBy=[
                    {"Type": "TAG", "Key": self._config.cluster_tag_key},
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"Cost Explorer query failed: {exc}") from exc

        for period in results:
            p_start = date.fromisoformat(period["TimePeriod"]["Start"])
            p_end = date.fromisoformat(period["TimePeriod"]["End"])
            for group in period.get("Groups", []):
                record = self._map_group(group, p_start, p_end)
                if record is not None:
                    yield record

    def _build_filter(self) -> dict[str, Any]:
        """Cost Explorer filter restricting to Databricks-tagged resources."""
        conditions = [
            {"Tags": {"Key": key, "Values": [value]}}
            for key, value in self._config.tag_filters.items()
        ]
        if not conditions:
            return {}
        if len(conditions) == 1:
            return conditions[0]
        return {"And": conditions}

    def _paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            resp = self._ce.get_cost_and_usage(**kwargs)
            out.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                return out
            kwargs["NextPageToken"] = token

    def _map_group(
        self, group: dict[str, Any], p_start: date, p_end: date
    ) -> FocusRecord | None:
        keys = group.get("Keys", [])
        # Key[0] is "ClusterId$<value>"; Key[1] is the service dimension.
        tag_value = keys[0].split("$", 1)[1] if keys and "$" in keys[0] else ""
        service_raw = keys[1] if len(keys) > 1 else ""
        category, service_name = _SERVICE_MAP.get(service_raw, (ServiceCategory.OTHER, service_raw))
        cost = to_decimal(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount"))
        if cost == 0:
            return None
        tags = {self._config.cluster_tag_key: tag_value} if tag_value else {}
        return FocusRecord(
            provider_name=ProviderName.AWS,
            billing_account_id="aws-infra",
            billing_period_start=p_start.replace(day=1),
            billing_period_end=p_end,
            charge_period_start=_dt(p_start),
            charge_period_end=_dt(p_end),
            billed_cost=cost,
            effective_cost=cost,
            list_cost=cost,
            contracted_cost=cost,
            charge_category=ChargeCategory.USAGE,
            charge_description=f"{service_name} backing Databricks cluster {tag_value}",
            service_category=category,
            service_name=service_name,
            resource_id=tag_value or None,
            tags=tags,
            x_source_connector=self.name,
        )


def _next_day(d: date) -> date:
    from datetime import timedelta

    return d + timedelta(days=1)


def _dt(d: date):  # type: ignore[no-untyped-def]
    from datetime import datetime

    return datetime(d.year, d.month, d.day)
