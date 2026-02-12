"""Billing aggregation into billing_resource_monthly.

Groups raw billing_records by the appropriate resource grain based on
which ID columns are populated, resolves human-readable names from
inventory tables, and writes pre-aggregated rows for fast dashboard queries.
"""

from __future__ import annotations

import uuid
from datetime import date

import structlog
from sqlmodel import Session, delete, func, select

from auralake_backend.db.models import (
    BillingRecord,
    BillingResourceMonthly,
    ComputeResourceRecord,
)

logger = structlog.get_logger(__name__)


def _month_trunc(d: date) -> date:
    """Truncate a date to the 1st of its month."""
    return d.replace(day=1)


class BillingAggregator:
    """Delete-and-replace aggregated billing for a connection."""

    def __init__(self, connection_id: uuid.UUID, session: Session) -> None:
        self.connection_id = connection_id
        self.session = session

    def run(self) -> int:
        """Aggregate all billing records. Returns total rows inserted."""
        # Delete existing aggregated rows for this connection
        self.session.execute(
            delete(BillingResourceMonthly).where(
                BillingResourceMonthly.connection_id == self.connection_id
            )
        )

        count = 0
        count += self._aggregate_jobs()
        count += self._aggregate_clusters()
        count += self._aggregate_warehouses()
        count += self._aggregate_pipelines()
        count += self._aggregate_endpoints()

        self.session.flush()
        logger.info(
            "billing_aggregation_completed",
            connection_id=str(self.connection_id),
            rows=count,
        )
        return count

    # ------------------------------------------------------------------
    # Jobs: group by COALESCE(job_name, job_id) per SKU + month
    # Records with job_id set are job charges regardless of SKU name.
    # ------------------------------------------------------------------

    def _aggregate_jobs(self) -> int:
        month_col = func.date_trunc("month", BillingRecord.usage_date)
        resource_key_col = func.coalesce(BillingRecord.job_name, BillingRecord.job_id)

        rows = self.session.exec(
            select(
                BillingRecord.sku,
                month_col.label("month"),
                resource_key_col.label("resource_key"),
                BillingRecord.job_name,
                func.sum(BillingRecord.dbu_usage),
                func.sum(BillingRecord.cost_usd),
                func.array_agg(func.distinct(BillingRecord.job_id)),
            )
            .where(
                BillingRecord.connection_id == self.connection_id,
                BillingRecord.job_id.isnot(None),  # type: ignore[union-attr]
            )
            .group_by(BillingRecord.sku, month_col, resource_key_col, BillingRecord.job_name)
        ).all()

        count = 0
        for row in rows:
            job_ids = [jid for jid in (row[6] or []) if jid is not None]
            self.session.add(
                BillingResourceMonthly(
                    connection_id=self.connection_id,
                    month=_month_trunc(row[1].date() if hasattr(row[1], "date") else row[1]),
                    sku=row[0],
                    resource_type="job",
                    resource_key=str(row[2]) if row[2] else "unknown",
                    resource_name=row[3],  # job_name (may be None)
                    creator=None,
                    dbu_usage=float(row[4] or 0),
                    cost_usd=float(row[5] or 0),
                    resource_ids=job_ids,
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # All-purpose clusters: group by creator (user email) per SKU + month
    # Records with cluster_id but no job_id are interactive cluster usage.
    # ------------------------------------------------------------------

    def _aggregate_clusters(self) -> int:
        cluster_info = self._get_cluster_info()

        month_col = func.date_trunc("month", BillingRecord.usage_date)

        rows = self.session.exec(
            select(
                BillingRecord.sku,
                month_col.label("month"),
                BillingRecord.cluster_id,
                func.sum(BillingRecord.dbu_usage),
                func.sum(BillingRecord.cost_usd),
            )
            .where(
                BillingRecord.connection_id == self.connection_id,
                BillingRecord.cluster_id.isnot(None),  # type: ignore[union-attr]
                BillingRecord.job_id.is_(None),  # type: ignore[union-attr]
            )
            .group_by(BillingRecord.sku, month_col, BillingRecord.cluster_id)
        ).all()

        # Re-group by creator (one user may have multiple cluster_ids)
        grouped: dict[tuple[str, date, str], dict] = {}
        for row in rows:
            cluster_id = row[2]
            name, creator = cluster_info.get(cluster_id, (None, None))
            group_key = creator or cluster_id  # group by creator, fallback to cluster_id
            month = _month_trunc(row[1].date() if hasattr(row[1], "date") else row[1])
            key = (row[0], month, group_key)

            if key not in grouped:
                grouped[key] = {
                    "dbu": 0.0,
                    "cost": 0.0,
                    "cluster_ids": set(),
                    "names": set(),
                    "creator": creator,
                }
            grouped[key]["dbu"] += float(row[3] or 0)
            grouped[key]["cost"] += float(row[4] or 0)
            grouped[key]["cluster_ids"].add(cluster_id)
            if name:
                grouped[key]["names"].add(name)

        count = 0
        for (sku, month, group_key), data in grouped.items():
            creator = data["creator"]
            names = sorted(data["names"])
            # Build display name: "name (creator)" if both, name if only name,
            # creator if only creator, cluster_id as last fallback
            if names and creator:
                display_name = f"{', '.join(names)} ({creator})"
            elif names:
                display_name = ", ".join(names)
            elif creator:
                display_name = creator
            else:
                display_name = group_key

            self.session.add(
                BillingResourceMonthly(
                    connection_id=self.connection_id,
                    month=month,
                    sku=sku,
                    resource_type="cluster",
                    resource_key=group_key,
                    resource_name=display_name,
                    creator=creator,
                    dbu_usage=data["dbu"],
                    cost_usd=data["cost"],
                    resource_ids=sorted(data["cluster_ids"]),
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # SQL warehouses: group by warehouse_id per SKU + month
    # ------------------------------------------------------------------

    def _aggregate_warehouses(self) -> int:
        wh_info = self._get_warehouse_info()

        month_col = func.date_trunc("month", BillingRecord.usage_date)

        rows = self.session.exec(
            select(
                BillingRecord.sku,
                month_col.label("month"),
                BillingRecord.warehouse_id,
                func.sum(BillingRecord.dbu_usage),
                func.sum(BillingRecord.cost_usd),
            )
            .where(
                BillingRecord.connection_id == self.connection_id,
                BillingRecord.warehouse_id.isnot(None),  # type: ignore[union-attr]
            )
            .group_by(BillingRecord.sku, month_col, BillingRecord.warehouse_id)
        ).all()

        count = 0
        for row in rows:
            wh_id = row[2]
            name, creator = wh_info.get(wh_id, (wh_id, None))
            display_name = f"{name} ({creator})" if creator else name
            self.session.add(
                BillingResourceMonthly(
                    connection_id=self.connection_id,
                    month=_month_trunc(row[1].date() if hasattr(row[1], "date") else row[1]),
                    sku=row[0],
                    resource_type="warehouse",
                    resource_key=wh_id,
                    resource_name=display_name,
                    creator=creator,
                    dbu_usage=float(row[3] or 0),
                    cost_usd=float(row[4] or 0),
                    resource_ids=[wh_id],
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # DLT pipelines: group by pipeline_id per SKU + month
    # ------------------------------------------------------------------

    def _aggregate_pipelines(self) -> int:
        pl_info = self._get_pipeline_info()

        month_col = func.date_trunc("month", BillingRecord.usage_date)

        rows = self.session.exec(
            select(
                BillingRecord.sku,
                month_col.label("month"),
                BillingRecord.pipeline_id,
                func.sum(BillingRecord.dbu_usage),
                func.sum(BillingRecord.cost_usd),
            )
            .where(
                BillingRecord.connection_id == self.connection_id,
                BillingRecord.pipeline_id.isnot(None),  # type: ignore[union-attr]
            )
            .group_by(BillingRecord.sku, month_col, BillingRecord.pipeline_id)
        ).all()

        count = 0
        for row in rows:
            pl_id = row[2]
            name, creator = pl_info.get(pl_id, (pl_id, None))
            self.session.add(
                BillingResourceMonthly(
                    connection_id=self.connection_id,
                    month=_month_trunc(row[1].date() if hasattr(row[1], "date") else row[1]),
                    sku=row[0],
                    resource_type="pipeline",
                    resource_key=pl_id,
                    resource_name=name,
                    creator=creator,
                    dbu_usage=float(row[3] or 0),
                    cost_usd=float(row[4] or 0),
                    resource_ids=[pl_id],
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Serving endpoints: group by endpoint_id per SKU + month
    # ------------------------------------------------------------------

    def _aggregate_endpoints(self) -> int:
        ep_info = self._get_endpoint_info()

        month_col = func.date_trunc("month", BillingRecord.usage_date)

        rows = self.session.exec(
            select(
                BillingRecord.sku,
                month_col.label("month"),
                BillingRecord.endpoint_id,
                func.sum(BillingRecord.dbu_usage),
                func.sum(BillingRecord.cost_usd),
            )
            .where(
                BillingRecord.connection_id == self.connection_id,
                BillingRecord.endpoint_id.isnot(None),  # type: ignore[union-attr]
            )
            .group_by(BillingRecord.sku, month_col, BillingRecord.endpoint_id)
        ).all()

        count = 0
        for row in rows:
            ep_id = row[2]
            name, creator = ep_info.get(ep_id, (ep_id, None))
            self.session.add(
                BillingResourceMonthly(
                    connection_id=self.connection_id,
                    month=_month_trunc(row[1].date() if hasattr(row[1], "date") else row[1]),
                    sku=row[0],
                    resource_type="endpoint",
                    resource_key=ep_id,
                    resource_name=name,
                    creator=creator,
                    dbu_usage=float(row[3] or 0),
                    cost_usd=float(row[4] or 0),
                    resource_ids=[ep_id],
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Inventory lookups
    # ------------------------------------------------------------------

    def _get_cluster_info(self) -> dict[str, tuple[str | None, str | None]]:
        """Return cluster_id -> (name, creator) mapping."""
        rows = self.session.exec(
            select(
                ComputeResourceRecord.resource_id,
                ComputeResourceRecord.resource_name,
                ComputeResourceRecord.creator,
            ).where(
                ComputeResourceRecord.connection_id == self.connection_id,
                ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                    ["all_purpose_cluster", "job_cluster"]
                ),
            )
        ).all()
        return {r[0]: (r[1], r[2]) for r in rows}

    def _get_warehouse_info(self) -> dict[str, tuple[str, str | None]]:
        """Return warehouse_id -> (name, creator) mapping."""
        rows = self.session.exec(
            select(
                ComputeResourceRecord.resource_id,
                ComputeResourceRecord.resource_name,
                ComputeResourceRecord.creator,
            ).where(
                ComputeResourceRecord.connection_id == self.connection_id,
                ComputeResourceRecord.resource_type == "sql_warehouse",
            )
        ).all()
        return {r[0]: (r[1], r[2]) for r in rows}

    def _get_pipeline_info(self) -> dict[str, tuple[str, str | None]]:
        """Return pipeline_id -> (name, creator) mapping."""
        rows = self.session.exec(
            select(
                ComputeResourceRecord.resource_id,
                ComputeResourceRecord.resource_name,
                ComputeResourceRecord.creator,
            ).where(
                ComputeResourceRecord.connection_id == self.connection_id,
                ComputeResourceRecord.resource_type == "dlt_pipeline",
            )
        ).all()
        return {r[0]: (r[1], r[2]) for r in rows}

    def _get_endpoint_info(self) -> dict[str, tuple[str, str | None]]:
        """Return endpoint_id -> (name, creator) mapping."""
        rows = self.session.exec(
            select(
                ComputeResourceRecord.resource_id,
                ComputeResourceRecord.resource_name,
                ComputeResourceRecord.creator,
            ).where(
                ComputeResourceRecord.connection_id == self.connection_id,
                ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                    ["serving_endpoint", "vector_search_endpoint"]
                ),
            )
        ).all()
        return {r[0]: (r[1], r[2]) for r in rows}
