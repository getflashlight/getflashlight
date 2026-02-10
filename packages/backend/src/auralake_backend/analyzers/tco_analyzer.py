"""Total cost of ownership -- combines DBU + AWS infrastructure costs."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from auralake_shared.models.billing import TCORecord
from auralake_shared.models.recommendations import AnalysisResult

from auralake_backend.analyzers.base import AbstractAnalyzer


class TCOAnalyzer(AbstractAnalyzer):
    name = "tco"

    def analyze(self) -> AnalysisResult:
        cost_client = self.context.provider.get_cost_client()
        infra_client = self.context.provider.get_infra_cost_client()
        days = self.context.config.defaults.lookback_days
        end = date.today()
        start = end - timedelta(days=days)

        # Get DBU costs per cluster
        breakdown = cost_client.get_cost_breakdown(start, end)

        # Get AWS costs
        compute_costs = infra_client.get_compute_costs(start, end)
        storage_costs = infra_client.get_storage_costs(start, end)
        transfer_costs = infra_client.get_data_transfer_costs(start, end)

        # Get resource mappings
        mappings = infra_client.map_platform_resources_to_infra()
        cluster_to_ec2: dict[str, list[str]] = {}
        for m in mappings:
            if m.platform_resource_type == "cluster":
                cluster_to_ec2.setdefault(m.platform_resource_id, []).append(m.infra_resource_id)

        # Build TCO records per cluster
        ec2_cost_by_resource: dict[str, Decimal] = {}
        for c in compute_costs:
            ec2_cost_by_resource[c.resource_id] = (
                ec2_cost_by_resource.get(c.resource_id, Decimal("0")) + c.cost_usd
            )

        all_cluster_ids = set(breakdown.by_cluster.keys()) | set(cluster_to_ec2.keys())
        tco_records = []
        for cluster_id in all_cluster_ids:
            dbu_cost = breakdown.by_cluster.get(cluster_id, Decimal("0"))
            ec2_cost = Decimal("0")
            for ec2_id in cluster_to_ec2.get(cluster_id, []):
                ec2_cost += ec2_cost_by_resource.get(ec2_id, Decimal("0"))

            tco_records.append(
                TCORecord(
                    resource_name=cluster_id,
                    resource_id=cluster_id,
                    dbu_cost=dbu_cost,
                    ec2_cost=ec2_cost,
                )
            )

        total_dbu = sum((r.dbu_cost for r in tco_records), Decimal("0"))
        total_ec2 = sum((r.ec2_cost for r in tco_records), Decimal("0"))
        total_storage = sum((s.cost_usd for s in storage_costs), Decimal("0"))
        total_transfer = sum((t.cost_usd for t in transfer_costs), Decimal("0"))

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=[],
            summary={
                "total_dbu_usd": str(total_dbu),
                "total_ec2_usd": str(total_ec2),
                "total_storage_usd": str(total_storage),
                "total_transfer_usd": str(total_transfer),
                "total_tco_usd": str(total_dbu + total_ec2 + total_storage + total_transfer),
                "cluster_count": len(tco_records),
                "tco_records": [r.model_dump() for r in tco_records],
            },
        )
