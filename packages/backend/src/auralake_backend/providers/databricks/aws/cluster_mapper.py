"""Maps Databricks cluster IDs to EC2 instances via tags."""

from __future__ import annotations

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.billing import ResourceMapping
from auralake_shared.models.config import DatabricksAWSConfig


class ClusterMapper:
    def __init__(self, ec2_client, aws_config: DatabricksAWSConfig) -> None:
        self._ec2 = ec2_client
        self._config = aws_config

    def map_clusters(self) -> list[ResourceMapping]:
        """Map Databricks clusters to EC2 instances using tags."""
        try:
            tag_key = self._config.cost_explorer.cluster_tag_key
            tag_filters = self._config.cost_explorer.tag_filters

            filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in tag_filters.items()]

            paginator = self._ec2.get_paginator("describe_instances")
            mappings = []
            for page in paginator.paginate(Filters=filters):
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                        cluster_id = tags.get(tag_key)
                        if cluster_id:
                            mappings.append(
                                ResourceMapping(
                                    platform_resource_type="cluster",
                                    platform_resource_id=cluster_id,
                                    infra_resource_type="ec2_instance",
                                    infra_resource_id=instance["InstanceId"],
                                    tags=tags,
                                )
                            )
            return mappings
        except Exception as exc:
            raise APIError(
                "databricks",
                f"Cluster-to-EC2 mapping failed: {exc}",
            ) from exc
