"""S3 storage analysis helpers."""

from __future__ import annotations

from auralake_shared.core.exceptions import APIError


class S3Client:
    def __init__(self, s3_client) -> None:
        self._s3 = s3_client

    def list_buckets(self) -> list[dict]:
        try:
            return self._s3.list_buckets().get("Buckets", [])
        except Exception as exc:
            raise APIError("databricks", f"S3 list-buckets failed: {exc}") from exc

    def get_bucket_size(self, bucket: str) -> dict:
        """Get approximate bucket size via CloudWatch."""
        try:
            from datetime import datetime, timedelta

            import boto3

            cw = boto3.client("cloudwatch")
            response = cw.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                StartTime=datetime.utcnow() - timedelta(days=2),
                EndTime=datetime.utcnow(),
                Period=86400,
                Statistics=["Average"],
            )
            datapoints = response.get("Datapoints", [])
            size_bytes = datapoints[-1]["Average"] if datapoints else 0
            return {"bucket": bucket, "size_bytes": size_bytes}
        except Exception as exc:
            raise APIError("databricks", f"S3 bucket size query failed: {exc}") from exc

    def get_bucket_location(self, bucket: str) -> str:
        try:
            resp = self._s3.get_bucket_location(Bucket=bucket)
            return resp.get("LocationConstraint") or "us-east-1"
        except Exception as exc:
            raise APIError("databricks", f"S3 get-bucket-location failed: {exc}") from exc
