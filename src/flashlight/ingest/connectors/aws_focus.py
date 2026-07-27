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

from flashlight.core.exceptions import ConnectorError, FocusValidationError
from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.focus import sql_mapping
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.ingest.base import Connector, IngestWindow, ProgressCallback
from flashlight.ingest.config import AwsFocusConfig, aws_client, env
from flashlight.ingest.connectors._coerce import to_decimal
from flashlight.lake import bronze
from flashlight.lake import duck as lake_duck

logger = get_logger(__name__)

# FOCUS ServiceName for S3 (confirmed usage in aws_infra.py's coarser Cost Explorer path).
_S3_SERVICE_NAME = "Amazon Simple Storage Service"

# fetch_efficiency's S3 intelligent-tiering signal — aggregated straight out of this
# connector's own BRONZE rows (params: x_source_connector, service_name, window start,
# window end-exclusive). Grouping/SUM/bool_or happen in DuckDB; Python only ever sees
# one row per (bucket, month), not one row per line item.
_S3_TIERING_SQL = """
    SELECT
        resource_id,
        any_value(resource_name) AS name,
        date_trunc('month', charge_period_start)::DATE AS month,
        sum(billed_cost) AS cost,
        bool_or(
            lower(coalesce(charge_description, '') || ' ' || coalesce(sku_id, ''))
            LIKE '%intelligent-tiering%'
            OR lower(coalesce(charge_description, '') || ' ' || coalesce(sku_id, ''))
            LIKE '%intelligent tiering%'
        ) AS on_it
    FROM raw.focus_record
    WHERE x_source_connector = ?
      AND service_name = ?
      AND resource_id IS NOT NULL
      AND charge_period_start >= ?
      AND charge_period_start < ?
    GROUP BY resource_id, month
"""

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
        self._s3 = aws_client(
            "s3",
            region=config.region,
            profile=config.aws_profile,
            access_key_env=config.access_key_env,
            secret_key_env=config.secret_key_env,
        )

    def ingest(
        self,
        window: IngestWindow,
        *,
        run_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Vectorized bulk path: DuckDB reads the manifest-listed S3 Parquet with the
        window/service predicate pushed down, maps it (:mod:`flashlight.focus.sql_mapping`),
        and writes straight to BRONZE — no FocusRecord objects, no per-row Python.
        """
        files = self._manifest_files(window)
        if not files:
            return 0

        con = duckdb.connect()
        try:
            if any(f.startswith("s3://") for f in files):
                con.execute("INSTALL httpfs; LOAD httpfs;")
                con.execute("SET http_timeout = 180;")
                con.execute(self._s3_secret_sql())
            sql_mapping.ensure_helpers(con)
            con.execute(
                "CREATE OR REPLACE MACRO redshift_charge_text(d, s) AS "
                "lower(coalesce(d, '') || ' ' || coalesce(s, ''))"
            )
            source_sql = (
                f"(SELECT * FROM {_scan_source(files)} "
                f"WHERE {_scan_where_literal(self._allowed, window)})"
            )
            present = sql_mapping.present_columns(con, source_sql)
            mapped = sql_mapping.mapping_sql(
                source_sql,
                connector=self.name,
                run_id=run_id,
                # AWS Data Exports emit FOCUS 1.2; the shared mapper defaults to 1.1,
                # which it can't know per-source.
                focus_version="1.2",
                present=present,
                cost_subcategory_sql=_redshift_subcategory_sql(),
            )
            written = bronze.write_window_sql(
                self.name, window, con, mapped, base_currency=get_settings().base_currency
            )
        except (ConnectorError, FocusValidationError):
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a connector failure
            raise ConnectorError(self.name, f"DuckDB ingest failed: {exc}") from exc
        finally:
            con.close()
        logger.info("aws_focus_ingest_done", files=len(files), rows=written)
        return written

    def _manifest_files(self, window: IngestWindow) -> list[str]:
        """Current-version S3 Parquet keys for every billing period overlapping
        ``window``, read from each period's manifest (not a ``*.parquet`` glob — see
        the module docstring for why)."""
        manifests = {
            period: key
            for period, key in self._list_partition_manifests().items()
            if _period_in_window(period, window)
        }
        if not manifests:
            logger.warning("aws_focus_no_manifests", prefix=self._config.s3_prefix)
            return []

        files: list[str] = []
        for period, manifest_key in sorted(manifests.items()):
            manifest = self._read_manifest(manifest_key)
            files.extend(_extract_data_file_keys(manifest, self._config.s3_bucket, manifest_key))
        files = list(dict.fromkeys(files))  # de-dup, preserve order
        if not files:
            logger.warning("aws_focus_manifest_no_files", periods=sorted(manifests))
        return files

    def fetch_efficiency(self, window: IngestWindow) -> Iterator[EfficiencyRecord]:
        """S3 storage-tiering signal, read from this connector's own BRONZE rows —
        ``ingest()`` already wrote them for this window moments earlier in the same
        run (``ingest/runner.py`` runs every connector's cost pull to completion
        before any connector's ``fetch_efficiency()`` runs), so this is a local
        Parquet read, not a second S3 fetch. Groups S3 line items by (bucket,
        month); a bucket is a tiering candidate unless any of its rows mention
        Intelligent-Tiering in ChargeDescription/SkuId.

        Text-match heuristic, not a real storage-class field — see the
        ``s3_intelligent_tiering`` rule in ``efficiency/waste_rules.py`` for the
        not-yet-validated-against-a-live-export caveat.
        """
        con = lake_duck.connect()
        try:
            lake_duck.register_bronze(con)
            rows = con.execute(
                _S3_TIERING_SQL,
                [self.name, _S3_SERVICE_NAME, window.start, window.end + timedelta(days=1)],
            ).fetchall()
            columns = [d[0] for d in con.description]
        finally:
            con.close()

        fetched = mapped = 0
        for values in rows:
            group = dict(zip(columns, values, strict=True))
            fetched += 1
            cost = to_decimal(group["cost"])
            if cost <= 0:
                continue
            mapped += 1
            yield EfficiencyRecord(
                provider_name="AWS",
                charge_month=group["month"],
                entity_type=EntityType.STORAGE,
                entity_id=group["resource_id"],
                entity_name=group["name"],
                billed_cost=cost,
                cause_detail={
                    "storage_class": "intelligent_tiering" if group["on_it"] else "standard"
                },
                x_source_connector=self.name,
            )
        logger.info("aws_focus_efficiency_done", buckets_fetched=fetched, buckets_mapped=mapped)

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

    def _s3_secret_sql(self) -> str:
        region = _sql_str(self._config.region)
        # PATH style, not DuckDB's default VHOST — confirmed by hand (see
        # scratchpad validation): a Data Export whose destination prefix was set
        # with a leading "/" delivers keys like "/focus/.../file.parquet", and
        # DuckDB's VHOST-style S3 signer computes a mismatched signature
        # (SignatureDoesNotMatch) against the resulting double-slash URL, while
        # PATH style (bucket in the URL path, not the host) signs the identical
        # key correctly. boto3 isn't affected either way — this is purely a
        # DuckDB httpfs quirk. PATH style is valid for any real S3 bucket
        # regardless of key shape, so applying it unconditionally costs nothing.
        url_style = ", URL_STYLE 'path'"
        if self._config.aws_profile:
            # DuckDB's own credential_chain provider doesn't know how to pick a named
            # profile (incl. SSO profiles needing a role-assume) — resolve real,
            # possibly-temporary credentials via the same boto3 profile the S3 client
            # above uses, then hand DuckDB the frozen key/secret/token directly.
            creds = boto3.Session(profile_name=self._config.aws_profile).get_credentials()
            if creds is None:
                profile = self._config.aws_profile
                raise ConnectorError(self.name, f"no credentials for aws_profile {profile!r}")
            frozen = creds.get_frozen_credentials()
            token_clause = f", SESSION_TOKEN {_sql_str(frozen.token)}" if frozen.token else ""
            return (
                f"CREATE SECRET ( TYPE s3, KEY_ID {_sql_str(frozen.access_key)}, "
                f"SECRET {_sql_str(frozen.secret_key)}{token_clause}, "
                f"REGION {region}{url_style} )"
            )
        key = env(self._config.access_key_env)
        secret = env(self._config.secret_key_env)
        if key and secret:
            return (
                f"CREATE SECRET ( TYPE s3, KEY_ID {_sql_str(key)}, "
                f"SECRET {_sql_str(secret)}, REGION {region}{url_style} )"
            )
        # No static creds → fall back to the instance/role credential chain.
        return f"CREATE SECRET ( TYPE s3, PROVIDER credential_chain, REGION {region}{url_style} )"


# Ordered (most-specific-first), first match wins: category -> keywords looked for
# in lower(ChargeDescription + ' ' + SkuId). Single source of truth for BOTH
# _classify_redshift_cost_category (Python, unit-tested directly) and
# _redshift_subcategory_sql (the SQL CASE expression the vectorized ingest path
# actually runs) — generating the SQL from this table, rather than hand-writing a
# second copy of the same precedence, is what keeps the two from ever drifting
# apart the way lake/seed.py's mapping once drifted from _focus_map.py's.
#
# "compute"/"spectrum_scan" keywords are wider than they look — confirmed against a
# real account's actual billing text, not guessed: AWS's own compute line items read
# "$X hourly fee per Redshift, ra3.4xlarge instance" and "...ra3.4xlarge reserved
# instance applied" (no "compute"/"node" ever appears), and Spectrum's per-scan
# pricing reads "...for Redshift Data Scan" (no "spectrum"). Without "instance"/
# "data scan", real compute/scan usage silently fell into "other" — confirmed: it was
# the single largest line item in the account ($44K), not a rounding-error residual.
# "committed" is its own bucket, not "compute": an RI/Savings-Plan commitment isn't
# usage of any kind, and lumping it into "other" hid a distinct $20K+ signal.
_REDSHIFT_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spectrum_scan", ("spectrum", "data scan")),
    ("concurrency_scaling", ("concurrency",)),
    ("serverless", ("serverless",)),
    ("storage", ("storage", "backup", "snapshot")),
    ("committed", ("unused commitment",)),
    ("compute", ("compute", "node", "cluster", "instance")),
)


def _classify_redshift_cost_category(charge_description: str | None, sku_id: str | None) -> str:
    """Bucket a Redshift charge below SKU granularity: compute / concurrency-scaling /
    storage / spectrum-scan / serverless / other.

    Text-match heuristic over FOCUS-carried fields (mirrors the S3 intelligent-tiering
    signal in ``fetch_efficiency`` above) — AWS FOCUS exports carry no dedicated cost-
    category column, so this reads ChargeDescription/SkuId the same tolerant way.
    """
    text = f"{charge_description or ''} {sku_id or ''}".lower()
    for category, keywords in _REDSHIFT_CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return "other"


def _redshift_category_branch(category: str, keywords: tuple[str, ...]) -> str:
    text_expr = "redshift_charge_text(nz(ChargeDescription), nz(SkuId))"
    conditions = " OR ".join(f"{text_expr} LIKE '%{keyword}%'" for keyword in keywords)
    return f"WHEN {conditions} THEN '{category}'"


def _redshift_subcategory_sql() -> str:
    """SQL port of :func:`_classify_redshift_cost_category`, generated from
    :data:`_REDSHIFT_CATEGORY_RULES` — the ``x_cost_subcategory`` expression spliced
    into :func:`flashlight.focus.sql_mapping.mapping_sql`'s ``cost_subcategory_sql``.
    Evaluated over the raw ``nz(...)`` source columns, gated to Redshift's own FOCUS
    ServiceName values (every other row gets NULL, unchanged).

    A ``Credit`` row, or one classified ``committed``, gets NULL (excluded from
    ``gold.spend_by_cost_subcategory_month`` entirely — see its ``WHERE
    x_cost_subcategory IS NOT NULL``), not folded into a real bucket: this view is a
    usage-mix breakdown (compute vs storage vs …), and neither a credit nor an RI/
    Savings-Plan commitment charge is usage of any kind — text-matching a credit is
    also a dead end (its own description, e.g. "Acme Corp Goodwill Credits, credit
    id: …", carries no hint of what it was credited against). ``committed`` still exists
    as a real :data:`_REDSHIFT_CATEGORY_RULES` category (``_classify_redshift_cost_
    category`` returns it) — it's excluded only from this one usage-mix view, not
    reclassified as 'other'; it's already its own KPI on the Redshift dashboard page.
    """
    services = ", ".join(_sql_str(s) for s in sorted(REDSHIFT_SERVICE_NAMES))
    branches = "\n            ".join(
        _redshift_category_branch(category, keywords)
        for category, keywords in _REDSHIFT_CATEGORY_RULES
    )
    classified = f"""
    CASE WHEN coalesce(nz(ServiceName), 'Unknown') IN ({services}) THEN
        CASE
            WHEN nz(ChargeCategory) = 'Credit' THEN NULL
            {branches}
            ELSE 'other'
        END
    ELSE NULL END
    """
    return f"NULLIF(({classified}), 'committed')"


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


def _parquet_array(files: list[str]) -> str:
    return "[" + ", ".join(_sql_str(f) for f in files) + "]"


def _scan_source(files: list[str]) -> str:
    """The bare ``read_parquet(...)`` table expression for the vectorized path."""
    return f"read_parquet({_parquet_array(files)}, union_by_name=true)"


def _scan_where_literal(allowed: set[str], window: IngestWindow) -> str:
    """The ``ingest()`` window/service predicate, with literals inlined instead of
    ``$``-params: ``window`` and ``allowed`` come from internal config (the ingest
    window, the connections.yml allow-list), never free-form user input, so
    inlining is safe — and it's what lets this predicate be embedded inside a
    ``CREATE TABLE AS`` several layers deep in :mod:`sql_mapping`'s CTEs.
    """
    clauses = [
        f'"ChargePeriodStart" >= DATE {_sql_str(window.start.isoformat())}',
        f'"ChargePeriodStart" < (DATE {_sql_str(window.end.isoformat())} + INTERVAL 1 DAY)',
    ]
    if allowed:
        services = ", ".join(_sql_str(s) for s in sorted(allowed))
        clauses.append(f'"ServiceName" IN ({services})')
    return " AND ".join(clauses)


def _sql_str(value: str) -> str:
    """Single-quote a string literal for inlining into SQL (escapes quotes)."""
    return "'" + value.replace("'", "''") + "'"
