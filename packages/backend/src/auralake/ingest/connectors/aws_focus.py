"""AWS native FOCUS export connector.

AWS Data Exports emit FOCUS Parquet to S3. Because the export is already FOCUS,
this connector just reads the Parquet objects and hands each row to the shared
FOCUS mapper. EC2/EBS/S3 lines (the infra backing Databricks classic compute) are
included, which is exactly what the TCO join needs.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import boto3
import pyarrow.parquet as pq

from auralake.core.exceptions import ConnectorError
from auralake.core.logging import get_logger
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import AwsFocusConfig, env
from auralake.ingest.connectors._focus_map import map_focus_row

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
            record = map_focus_row(row, self.name)
            if record is not None and _in_window(record, window):
                yield record


def _in_window(record: FocusRecord, window: IngestWindow) -> bool:
    return (window.start <= record.billing_period_start <= window.end) or (
        window.start <= record.charge_period_start.date() <= window.end
    )
