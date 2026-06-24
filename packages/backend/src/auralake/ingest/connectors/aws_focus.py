"""AWS native FOCUS export connector — manifest-driven, DuckDB pushdown scan.

AWS Data Exports writes FOCUS Parquet to S3 under an export root:

    <prefix>/<export-name>/data/BILLING_PERIOD=YYYY-MM/...<chunk>.snappy.parquet
    <prefix>/<export-name>/metadata/BILLING_PERIOD=YYYY-MM/<...>Manifest.json

The per-partition ``Manifest.json`` always names the *current* version of that
billing period's data files — true for both "overwrite" and "create new" delivery
modes (in "create new", a fresh copy is also written into the partition folder on
every refresh). So we read the manifest and ingest exactly the files it lists,
rather than globbing ``*.parquet``; that's what stops a stale "create new"
execution from double-counting a billing period.

DuckDB then scans those Parquet files straight from S3 with column pruning and
predicate pushdown: the ``include_services`` allow-list and the charge-period
window become a WHERE clause, so a data-platform-scoped pull reads a fraction of
the bytes instead of pulling the whole account into memory. Postgres stays the
warehouse — DuckDB is only the S3-Parquet read engine for ingestion.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import boto3
import duckdb

from auralake.core.exceptions import ConnectorError
from auralake.core.logging import get_logger
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import AwsFocusConfig, env
from auralake.ingest.connectors._focus_map import map_focus_row

logger = get_logger(__name__)

# Partition-level manifest: ``metadata/BILLING_PERIOD=YYYY-MM/<...>Manifest.json``.
# The trailing ``[^/]*`` excludes the per-execution copies that "create new" mode
# nests one level deeper under ``<timestamp>-<execution-id>/``. Case-insensitive:
# AWS delivers the partition key lowercased (``billing_period=``).
_MANIFEST_RE = re.compile(r"metadata/billing_period=(\d{4}-\d{2})/[^/]*Manifest\.json$", re.I)


class AwsFocusConnector(Connector):
    name = "aws_focus"

    def __init__(self, config: AwsFocusConfig) -> None:
        self._config = config
        # Empty allow-list = every service; otherwise keep only these ServiceNames.
        self._allowed = set(config.include_services)
        self._s3 = boto3.client(
            "s3",
            region_name=config.region,
            aws_access_key_id=env(config.access_key_env),
            aws_secret_access_key=env(config.secret_key_env),
        )

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        manifests = {
            period: key
            for period, key in self._list_partition_manifests().items()
            if _period_in_window(period, window)
        }
        if not manifests:
            logger.warning("aws_focus_no_manifests", prefix=self._config.s3_prefix)
            return

        files: list[str] = []
        for period, manifest_key in sorted(manifests.items()):
            manifest = self._read_manifest(manifest_key)
            files.extend(_extract_data_file_keys(manifest, self._config.s3_bucket, manifest_key))
        files = list(dict.fromkeys(files))  # de-dup, preserve order
        if not files:
            logger.warning("aws_focus_manifest_no_files", periods=sorted(manifests))
            return

        logger.info("aws_focus_scan", periods=len(manifests), files=len(files))
        for row in self._scan(files, window):
            record = map_focus_row(row, self.name)
            if record is None or not _service_allowed(record, self._allowed):
                continue
            # AWS Data Exports emit FOCUS 1.2; stamp provenance (the shared mapper
            # defaults to 1.1, which it can't know per-source).
            yield record.model_copy(update={"x_focus_version": "1.2"})

    # ── S3 / manifest ────────────────────────────────────────────────────────
    def _list_partition_manifests(self) -> dict[str, str]:
        """Map billing period (YYYY-MM) → its partition-level manifest S3 key."""
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            manifests: dict[str, str] = {}
            for page in paginator.paginate(
                Bucket=self._config.s3_bucket, Prefix=self._config.s3_prefix
            ):
                for obj in page.get("Contents", []):
                    m = _MANIFEST_RE.search(obj["Key"])
                    if m:
                        manifests[m.group(1)] = obj["Key"]
            return manifests
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"S3 list failed: {exc}") from exc

    def _read_manifest(self, key: str) -> dict[str, Any]:
        try:
            body = self._s3.get_object(Bucket=self._config.s3_bucket, Key=key)["Body"].read()
            return json.loads(body)  # type: ignore[no-any-return]
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"manifest read failed for {key}: {exc}") from exc

    # ── DuckDB scan ──────────────────────────────────────────────────────────
    def _scan(self, files: list[str], window: IngestWindow) -> Iterator[dict[str, Any]]:
        try:
            con = duckdb.connect()
            con.execute("INSTALL httpfs; LOAD httpfs;")
            con.execute(self._s3_secret_sql())
            sql, params = _scan_sql(files, self._allowed, window)
            table = con.execute(sql, params).fetch_arrow_table()
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"DuckDB scan failed: {exc}") from exc
        yield from table.to_pylist()

    def _s3_secret_sql(self) -> str:
        region = _sql_str(self._config.region)
        key = env(self._config.access_key_env)
        secret = env(self._config.secret_key_env)
        if key and secret:
            return (
                f"CREATE SECRET ( TYPE s3, KEY_ID {_sql_str(key)}, "
                f"SECRET {_sql_str(secret)}, REGION {region} )"
            )
        # No static creds → fall back to the instance/role credential chain.
        return f"CREATE SECRET ( TYPE s3, PROVIDER credential_chain, REGION {region} )"


def _service_allowed(record: FocusRecord, allowed: set[str]) -> bool:
    """True if no allow-list is configured, or the row's ServiceName is in it."""
    return not allowed or record.service_name in allowed


def _period_in_window(period: str, window: IngestWindow) -> bool:
    """True if billing month ``YYYY-MM`` overlaps the inclusive [start, end] window."""
    year, month = (int(p) for p in period.split("-"))
    month_start = date(year, month, 1)
    month_end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return not (month_end < window.start or month_start > window.end)


def _extract_data_file_keys(manifest: dict[str, Any], bucket: str, manifest_key: str) -> list[str]:
    """Pull the current version's Parquet keys from a manifest, as ``s3://`` URLs.

    AWS doesn't publish the manifest's exact field names, so this is tolerant:
    prefer a ``dataFiles`` list (entries may carry ``key`` / ``url`` /
    ``relativePath``), and fall back to scanning the whole document for any
    ``.parquet`` string. Relative paths resolve against the manifest's own data
    partition (``metadata/`` → ``data/``).
    """
    data_dir = manifest_key.rsplit("/", 1)[0].replace("/metadata/", "/data/")
    raw: list[str] = []

    entries = manifest.get("dataFiles")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, str):
                raw.append(entry)
            elif isinstance(entry, dict):
                for field in ("key", "url", "s3Key", "relativePath"):
                    val = entry.get(field)
                    if isinstance(val, str) and val:
                        raw.append(val)
                        break
    if not raw:  # tolerant fallback: any .parquet string anywhere in the manifest
        raw = [s for s in _iter_strings(manifest) if s.endswith(".parquet")]

    urls: list[str] = []
    for value in raw:
        if value.startswith("s3://"):
            urls.append(value)
        elif "/" in value:  # bucket-relative key
            urls.append(f"s3://{bucket}/{value.lstrip('/')}")
        else:  # bare filename → resolve against the partition's data dir
            urls.append(f"s3://{bucket}/{data_dir}/{value}")
    return urls


def _iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_strings(item)


def _scan_sql(
    files: list[str], allowed: set[str], window: IngestWindow
) -> tuple[str, dict[str, Any]]:
    """Build the DuckDB scan with column-name quoting and pushdown predicates."""
    array = "[" + ", ".join(_sql_str(f) for f in files) + "]"
    sql = (
        f"SELECT * FROM read_parquet({array}, union_by_name=true) "
        'WHERE "ChargePeriodStart" >= $start '
        'AND "ChargePeriodStart" < ($end + INTERVAL 1 DAY)'
    )
    params: dict[str, Any] = {"start": window.start, "end": window.end}
    if allowed:
        sql += ' AND "ServiceName" = ANY($services)'
        params["services"] = sorted(allowed)
    return sql, params


def _sql_str(value: str) -> str:
    """Single-quote a string literal for inlining into SQL (escapes quotes)."""
    return "'" + value.replace("'", "''") + "'"
