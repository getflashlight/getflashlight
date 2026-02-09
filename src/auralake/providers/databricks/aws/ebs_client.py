"""EBS volume analysis -- orphaned volumes, volume type recommendations."""
from __future__ import annotations

from auralake.core.exceptions import APIError


class EBSClient:
    def __init__(self, ec2_client) -> None:
        self._ec2 = ec2_client

    def find_orphaned_volumes(self) -> list[dict]:
        """Find EBS volumes not attached to any instance."""
        try:
            paginator = self._ec2.get_paginator("describe_volumes")
            orphaned = []
            for page in paginator.paginate(
                Filters=[{"Name": "status", "Values": ["available"]}]
            ):
                for vol in page["Volumes"]:
                    orphaned.append(
                        {
                            "volume_id": vol["VolumeId"],
                            "size_gb": vol["Size"],
                            "volume_type": vol["VolumeType"],
                            "create_time": str(vol["CreateTime"]),
                            "tags": {
                                t["Key"]: t["Value"]
                                for t in vol.get("Tags", [])
                            },
                        }
                    )
            return orphaned
        except Exception as exc:
            raise APIError(
                "databricks",
                f"EBS orphaned volume scan failed: {exc}",
            ) from exc

    def get_volume_recommendations(self) -> list[dict]:
        """Identify gp2 volumes that should be gp3."""
        try:
            paginator = self._ec2.get_paginator("describe_volumes")
            recs = []
            for page in paginator.paginate(
                Filters=[{"Name": "volume-type", "Values": ["gp2"]}]
            ):
                for vol in page["Volumes"]:
                    recs.append(
                        {
                            "volume_id": vol["VolumeId"],
                            "current_type": "gp2",
                            "recommended_type": "gp3",
                            "size_gb": vol["Size"],
                            "reason": (
                                "gp3 is cheaper and faster than gp2 "
                                "for most workloads"
                            ),
                        }
                    )
            return recs
        except Exception as exc:
            raise APIError(
                "databricks",
                f"EBS volume recommendation scan failed: {exc}",
            ) from exc
