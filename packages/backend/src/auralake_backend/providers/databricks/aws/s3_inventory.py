"""S3 Inventory report client.

Configures S3 Inventory on source buckets, polls for completed reports,
and uses DuckDB to read Parquet inventory files directly from S3 before
pushing results to the Postgres application database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import S3InventoryConfig

logger = structlog.get_logger(__name__)


@dataclass
class S3ObjectRecord:
    """A single object from an S3 Inventory report."""

    bucket: str
    key: str
    size_bytes: int
    last_modified: datetime
    storage_class: str | None = None
    etag: str | None = None


class S3InventoryClient:
    """Configure, poll, and parse S3 Inventory reports using DuckDB."""

    def __init__(self, s3_client: Any, config: S3InventoryConfig) -> None:
        self._s3 = s3_client
        self._config = config

    def configure_inventory(
        self, source_bucket: str, inventory_id: str = "auralake"
    ) -> dict[str, Any]:
        """Call PutBucketInventoryConfiguration on the source bucket.

        Idempotent — overwrites any existing configuration with the same ID.
        """
        try:
            self._s3.put_bucket_inventory_configuration(
                Bucket=source_bucket,
                Id=inventory_id,
                InventoryConfiguration={
                    "Id": inventory_id,
                    "IsEnabled": True,
                    "Destination": {
                        "S3BucketDestination": {
                            "Bucket": (f"arn:aws:s3:::{self._config.destination_bucket}"),
                            "Format": "Parquet",
                            "Prefix": self._config.destination_prefix,
                        }
                    },
                    "Schedule": {"Frequency": self._config.frequency},
                    "IncludedObjectVersions": "Current",
                    "OptionalFields": self._config.included_fields,
                },
            )
            logger.info(
                "s3_inventory_configured",
                source_bucket=source_bucket,
                destination_bucket=self._config.destination_bucket,
                inventory_id=inventory_id,
            )
            return {
                "source_bucket": source_bucket,
                "destination_bucket": self._config.destination_bucket,
                "inventory_id": inventory_id,
            }
        except Exception as exc:
            raise APIError(
                "databricks",
                f"Failed to configure S3 Inventory on {source_bucket}: {exc}",
            ) from exc

    def get_latest_manifest(
        self,
        source_bucket: str,
        inventory_id: str = "auralake",
    ) -> dict[str, Any] | None:
        """Read the latest manifest.json from the inventory destination.

        Returns the parsed manifest dict or ``None`` if no report is available yet.
        """
        prefix = f"{self._config.destination_prefix}/{source_bucket}/{inventory_id}/"
        try:
            # List date-stamped folders to find the most recent report
            resp = self._s3.list_objects_v2(
                Bucket=self._config.destination_bucket,
                Prefix=prefix,
                Delimiter="/",
            )
            prefixes = sorted(
                (p["Prefix"] for p in resp.get("CommonPrefixes", [])),
                reverse=True,
            )
            if not prefixes:
                logger.info(
                    "s3_inventory_no_reports",
                    source_bucket=source_bucket,
                    prefix=prefix,
                )
                return None

            # The most recent date folder contains manifest.json
            latest_prefix = prefixes[0]
            manifest_key = f"{latest_prefix}manifest.json"
            obj = self._s3.get_object(
                Bucket=self._config.destination_bucket,
                Key=manifest_key,
            )
            manifest: dict[str, Any] = json.loads(obj["Body"].read().decode("utf-8"))
            manifest["_manifest_key"] = manifest_key
            logger.info(
                "s3_inventory_manifest_found",
                source_bucket=source_bucket,
                manifest_key=manifest_key,
            )
            return manifest

        except self._s3.exceptions.NoSuchKey:
            logger.info(
                "s3_inventory_manifest_not_found",
                source_bucket=source_bucket,
                prefix=prefix,
            )
            return None
        except Exception as exc:
            raise APIError(
                "databricks",
                f"Failed to read S3 Inventory manifest for {source_bucket}: {exc}",
            ) from exc

    def read_inventory_objects(
        self,
        manifest: dict[str, Any],
    ) -> list[S3ObjectRecord]:
        """Read Parquet inventory files listed in manifest using DuckDB.

        DuckDB reads Parquet directly from S3, which is far faster than
        downloading files and parsing with pyarrow.  Results are returned
        as lightweight dataclass records ready for DB insertion.
        """
        import duckdb

        files = manifest.get("files", [])
        if not files:
            return []

        dest_bucket = self._config.destination_bucket
        parquet_uris = [f"s3://{dest_bucket}/{f['key']}" for f in files]

        try:
            con = duckdb.connect()
            self._configure_duckdb_s3(con)

            records: list[S3ObjectRecord] = []
            for uri in parquet_uris:
                result = con.execute(
                    """
                    SELECT
                        bucket        AS bucket,
                        key           AS key,
                        size          AS size_bytes,
                        last_modified_date AS last_modified,
                        storage_class AS storage_class,
                        e_tag         AS etag
                    FROM read_parquet(?)
                    """,
                    [uri],
                ).fetchall()

                for row in result:
                    records.append(
                        S3ObjectRecord(
                            bucket=row[0],
                            key=row[1],
                            size_bytes=int(row[2]) if row[2] is not None else 0,
                            last_modified=(
                                row[3] if isinstance(row[3], datetime) else datetime.utcnow()
                            ),
                            storage_class=row[4],
                            etag=row[5],
                        )
                    )

            con.close()
            logger.info(
                "s3_inventory_parquet_read",
                file_count=len(parquet_uris),
                object_count=len(records),
            )
            return records

        except Exception as exc:
            raise APIError(
                "databricks",
                f"Failed to read S3 Inventory Parquet files via DuckDB: {exc}",
            ) from exc

    def _configure_duckdb_s3(self, con: Any) -> None:
        """Install httpfs and set S3 credentials in a DuckDB connection."""
        con.execute("INSTALL httpfs; LOAD httpfs;")

        # Pull credentials from the boto3 client's underlying session
        credentials = self._s3._endpoint.http_session._credentials  # noqa: SLF001
        if credentials is None:
            # Fall back to default credential chain (instance profile, etc.)
            return

        creds = credentials.get_frozen_credentials()
        if creds.access_key:
            con.execute(f"SET s3_access_key_id='{creds.access_key}';")
        if creds.secret_key:
            con.execute(f"SET s3_secret_access_key='{creds.secret_key}';")
        if creds.token:
            con.execute(f"SET s3_session_token='{creds.token}';")

        # Try to extract region from client config
        region = self._s3.meta.region_name
        if region:
            con.execute(f"SET s3_region='{region}';")
