"""EC2 helpers for instance info and spot pricing."""
from __future__ import annotations

from auralake.core.exceptions import APIError


class EC2Client:
    def __init__(self, ec2_client) -> None:
        self._ec2 = ec2_client

    def describe_instances(
        self, filters: list[dict] | None = None
    ) -> list[dict]:
        try:
            paginator = self._ec2.get_paginator("describe_instances")
            instances = []
            kwargs = {}
            if filters:
                kwargs["Filters"] = filters
            for page in paginator.paginate(**kwargs):
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instances.append(instance)
            return instances
        except Exception as exc:
            raise APIError(
                "databricks", f"EC2 describe-instances failed: {exc}"
            ) from exc

    def get_spot_price_history(
        self,
        instance_types: list[str],
        availability_zone: str | None = None,
    ) -> list[dict]:
        try:
            kwargs = {
                "InstanceTypes": instance_types,
                "ProductDescriptions": ["Linux/UNIX"],
                "MaxResults": 100,
            }
            if availability_zone:
                kwargs["AvailabilityZone"] = availability_zone
            response = self._ec2.describe_spot_price_history(**kwargs)
            return response.get("SpotPriceHistory", [])
        except Exception as exc:
            raise APIError(
                "databricks", f"EC2 spot price history failed: {exc}"
            ) from exc

    def describe_volumes(
        self, filters: list[dict] | None = None
    ) -> list[dict]:
        try:
            paginator = self._ec2.get_paginator("describe_volumes")
            volumes = []
            kwargs = {}
            if filters:
                kwargs["Filters"] = filters
            for page in paginator.paginate(**kwargs):
                volumes.extend(page["Volumes"])
            return volumes
        except Exception as exc:
            raise APIError(
                "databricks", f"EC2 describe-volumes failed: {exc}"
            ) from exc
