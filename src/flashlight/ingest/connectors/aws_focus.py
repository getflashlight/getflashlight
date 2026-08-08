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

``AwsFocusConfig.cost_source`` picks between this vectorized S3 path
("focus_export", default) and a Cost Explorer fallback ("cost_explorer") —
an explicit choice, not automatic detection: only a connection that opts into
Cost Explorer needs ``ce:GetCostAndUsage``. The CE path is coarser (account-
level ``SERVICE`` totals, no per-charge detail, no cost-subcategory
classification) and — since the old Databricks-cluster-tag attribution this
once supported (``aws_infra``) was dropped along with it — scoped only by
``include_services``, not by resource tag.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import boto3
import duckdb

from flashlight.core.exceptions import ConnectorError, FocusValidationError
from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.focus import sql_mapping
from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.ingest._s3_service_names import S3_SERVICE_NAMES
from flashlight.ingest.base import Connector, IngestWindow, ProgressCallback
from flashlight.ingest.config import AwsFocusConfig, aws_client, effective_connector_name, env
from flashlight.ingest.connectors._coerce import to_decimal
from flashlight.lake import bronze, paths
from flashlight.lake import duck as lake_duck

logger = get_logger(__name__)


def _sql_str(value: str) -> str:
    """Single-quote a string literal for inlining into SQL (escapes quotes)."""
    return "'" + value.replace("'", "''") + "'"


def _services_sql(names: frozenset[str]) -> str:
    """A ServiceName allow-list as a SQL literal list for an ``IN (...)`` predicate."""
    return ", ".join(_sql_str(s) for s in sorted(names))


def _tune_bulk_connection(con: duckdb.DuckDBPyConnection) -> None:
    """Settings the vectorized bulk path needs but ``duckdb.connect()`` doesn't give it.

    This connection is deliberately NOT :func:`flashlight.lake.duck.connect` — that one
    caps ``memory_limit`` low (a laptop running several readers at once) and this is the
    one write path that genuinely wants the headroom, since ``write_window_sql``
    materializes the whole mapped window before COPYing it.

    But the bare connection defaults are wrong here in two ways that only show up on a
    real export:

    * ``temp_directory`` defaults to **``.tmp``, relative to the process's current working
      directory** — so a backfill large enough to spill writes gigabytes into whatever
      directory the operator happened to run ``flashlight ingest`` from, ignores
      ``FLASHLIGHT_DUCKDB_TEMP_DIR`` entirely, and fails outright where the cwd isn't
      writable (a container run read-only). Point it at the lake's own spill dir, the
      same one every other connection uses. Best-effort, matching ``duck.connect``: an
      unwritable spill dir shouldn't fail an ingest that may not need to spill at all.
    * ``preserve_insertion_order`` defaults to true, which makes the partitioned COPY
      hold rows back to keep an order BRONZE has no use for — it's Hive-partitioned by
      (connector, charge_month) and every consumer aggregates. Measured ~12% off the COPY
      at 2M rows, and it lowers peak memory, which is the binding constraint here.
    """
    con.execute("SET preserve_insertion_order = false")
    temp_dir = paths.duckdb_temp_dir()
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("aws_focus_temp_dir_unwritable", path=str(temp_dir), error=str(exc))
        return
    con.execute(f"SET temp_directory = {_sql_str(str(temp_dir))}")


# The two service allow-lists as SQL literal lists, for the predicates below and the
# x_cost_subcategory classifier further down. Both come from internal constants, never
# from user input, so inlining is safe — and it's what lets the classifier's expression
# nest inside sql_mapping's CTEs (see _scan_where_literal's docstring for the same
# reasoning applied to the ingest window).
_REDSHIFT_SERVICES_SQL = _services_sql(REDSHIFT_SERVICE_NAMES)
_S3_SERVICES_SQL = _services_sql(S3_SERVICE_NAMES)

# fetch_efficiency's S3 intelligent-tiering signal — aggregated straight out of this
# connector's own BRONZE rows (params: x_source_connector, first/last charge_month in
# the window, window start, window end-exclusive; the S3 ServiceName list is inlined,
# see above). Grouping/SUM/bool_or happen in DuckDB; Python only ever sees one row per
# (bucket, month), not one row per line item. The charge_month bounds are the Hive
# partition column, so DuckDB prunes whole month directories before opening any file —
# the charge_period_start bounds alone would still force a read of every month this
# connector ever wrote.
_S3_TIERING_SQL = f"""
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
      AND service_name IN ({_S3_SERVICES_SQL})
      AND charge_month >= ?
      AND charge_month <= ?
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
        # Instance-level, shadowing the class constant above — the connection's own
        # chosen name, so BRONZE partitioning (x_source_connector=<name>/...) and the
        # runlog/dashboard stay distinct across multiple AWS-cost-source connections.
        self.name = effective_connector_name(config)
        # Empty allow-list = every service; otherwise keep only these ServiceNames.
        self._allowed = set(config.include_services)
        self._s3 = aws_client(
            "s3",
            region=config.region,
            profile=config.aws_profile,
            access_key_env=config.access_key_env,
            secret_key_env=config.secret_key_env,
        )
        self._ce = aws_client(
            "ce",
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
        """``cost_source="cost_explorer"``: drain :meth:`fetch` (Cost Explorer) through
        the inherited row-based writer. ``cost_source="focus_export"`` (default):
        vectorized bulk path — DuckDB reads the manifest-listed S3 Parquet with the
        window/service predicate pushed down, maps it
        (:mod:`flashlight.focus.sql_mapping`), and writes straight to BRONZE — no
        FocusRecord objects, no per-row Python.
        """
        if self._config.cost_source == "cost_explorer":
            return super().ingest(window, run_id=run_id, on_progress=on_progress)

        files = self._manifest_files(window)
        if not files:
            return 0
        # Everything below is one vectorized DuckDB pass — no per-row Python, so
        # no natural per-row progress hook — and against a real export this can
        # run for minutes with nothing else printed in between. These two lines
        # are the only visibility the live tail (dashboard sync / CLI) gets
        # during that stretch; without them "still listing/reading S3" and
        # "hung" look identical from the outside.
        logger.info("aws_focus_scan_start", files=len(files))

        con = duckdb.connect()
        try:
            _tune_bulk_connection(con)
            if any(f.startswith("s3://") for f in files):
                con.execute("INSTALL httpfs; LOAD httpfs;")
                con.execute("SET http_timeout = 180;")
                con.execute(self._s3_secret_sql())
            sql_mapping.ensure_helpers(con)
            con.execute(
                # The text every x_cost_subcategory branch matches against, as one
                # macro so the (long) coalesce isn't repeated per keyword. Shared by
                # the Redshift and S3 rule families — see _cost_subcategory_sql.
                "CREATE OR REPLACE MACRO charge_text(d, s) AS "
                "lower(coalesce(d, '') || ' ' || coalesce(s, ''))"
            )
            source_sql = (
                f"(SELECT * FROM {_scan_source(files)} "
                f"WHERE {_scan_where_literal(self._allowed, window)})"
            )
            present = sql_mapping.present_columns(con, source_sql)
            logger.info("aws_focus_writing_bronze", files=len(files))
            mapped = sql_mapping.mapping_sql(
                source_sql,
                connector=self.name,
                run_id=run_id,
                # AWS Data Exports emit FOCUS 1.2; the shared mapper defaults to 1.1,
                # which it can't know per-source.
                focus_version="1.2",
                present=present,
                cost_subcategory_sql=_cost_subcategory_sql(),
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
        logger.info("aws_focus_listing_manifests", prefix=self._config.s3_prefix)
        manifests = {
            period: key
            for period, key in self._list_partition_manifests().items()
            if _period_in_window(period, window)
        }
        if not manifests:
            logger.warning("aws_focus_no_manifests", prefix=self._config.s3_prefix)
            return []
        logger.info("aws_focus_manifests_found", periods=sorted(manifests))

        # Only reached when cost_source="focus_export" (ingest()'s branch), which the
        # config validator guarantees means s3_bucket is set.
        assert self._config.s3_bucket is not None
        files: list[str] = []
        for period, manifest_key in sorted(manifests.items()):
            manifest = self._read_manifest(manifest_key)
            files.extend(_extract_data_file_keys(manifest, self._config.s3_bucket, manifest_key))
        files = list(dict.fromkeys(files))  # de-dup, preserve order
        if not files:
            logger.warning("aws_focus_manifest_no_files", periods=sorted(manifests))
        return files

    # ── Cost Explorer path (cost_source="cost_explorer") ────────────────────
    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        """Coarse account-level cost via Cost Explorer, grouped by SERVICE + day —
        no per-charge detail, no cost-subcategory classification, no resource/tag
        dimension (the old Databricks-cluster-tag attribution this once supported,
        via ``aws_infra``, was dropped, not ported — see the module docstring).
        Only reached via :meth:`ingest`'s ``cost_source="cost_explorer"`` branch,
        draining this through the base class's row-based writer.
        """
        try:
            results = self._paginate(
                TimePeriod={
                    "Start": str(window.start),
                    "End": str(window.end + timedelta(days=1)),
                },
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                Filter=self._ce_filter(),
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"Cost Explorer query failed: {exc}") from exc

        for period in results:
            p_start = date.fromisoformat(period["TimePeriod"]["Start"])
            p_end = date.fromisoformat(period["TimePeriod"]["End"])
            for group in period.get("Groups", []):
                record = self._map_ce_group(group, p_start, p_end)
                if record is not None:
                    yield record

    def _ce_filter(self) -> dict[str, Any]:
        if not self._allowed:
            return {}
        return {"Dimensions": {"Key": "SERVICE", "Values": sorted(self._allowed)}}

    def _paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            resp = self._ce.get_cost_and_usage(**kwargs)
            out.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                return out
            kwargs["NextPageToken"] = token

    def _map_ce_group(
        self, group: dict[str, Any], p_start: date, p_end: date
    ) -> FocusRecord | None:
        keys = group.get("Keys", [])
        service_name = keys[0] if keys else "Unknown"
        cost = to_decimal(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount"))
        if cost == 0:
            return None
        category = (
            ServiceCategory.ANALYTICS
            if service_name in REDSHIFT_SERVICE_NAMES
            else ServiceCategory.OTHER
        )
        return FocusRecord(
            provider_name=ProviderName.AWS,
            billing_account_id="aws-cost-explorer",
            billing_period_start=p_start.replace(day=1),
            billing_period_end=p_end,
            charge_period_start=_dt(p_start),
            charge_period_end=_dt(p_end),
            billed_cost=cost,
            effective_cost=cost,
            list_cost=cost,
            contracted_cost=cost,
            charge_category=ChargeCategory.USAGE,
            charge_description=f"{service_name} (Cost Explorer)",
            service_category=category,
            service_name=service_name,
            x_source_connector=self.name,
        )

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
                [
                    self.name,
                    window.start.strftime("%Y-%m"),
                    window.end.strftime("%Y-%m"),
                    window.start,
                    window.end + timedelta(days=1),
                ],
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
        """Map billing period (YYYY-MM) → its partition-level manifest S3 key.

        Scans only the ``metadata/`` subtrees, not the whole export: the ``data/``
        siblings hold every Parquet chunk the export has ever written (thousands of
        keys, growing with its age) and none of them can match ``_MANIFEST_RE``.
        Falls back to listing the configured prefix whole if the narrow scan finds
        nothing, so an unanticipated layout still ingests — just slower.
        """
        try:
            manifests = self._scan_manifests(self._metadata_prefixes())
            return manifests or self._scan_manifests([self._config.s3_prefix])
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"S3 list failed: {exc}") from exc

    def _metadata_prefixes(self) -> list[str]:
        """Candidate ``metadata/`` prefixes: one per export-name dir directly under
        ``s3_prefix`` (the documented layout), plus ``s3_prefix`` itself in case it
        already points at an export root. One delimited LIST, no recursion."""
        root = self._config.s3_prefix.strip("/")
        base = f"{root}/" if root else ""
        resp = self._s3.list_objects_v2(
            Bucket=self._config.s3_bucket, Prefix=base, Delimiter="/"
        )
        nested = [
            f"{cp['Prefix']}metadata/"
            for cp in resp.get("CommonPrefixes", [])
            if isinstance(cp.get("Prefix"), str)
        ]
        return [f"{base}metadata/", *nested]

    def _scan_manifests(self, prefixes: list[str]) -> dict[str, str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        manifests: dict[str, str] = {}
        for prefix in prefixes:
            for page in paginator.paginate(Bucket=self._config.s3_bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    m = _MANIFEST_RE.search(obj["Key"])
                    if m:
                        manifests[m.group(1)] = obj["Key"]
        return manifests

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
    ("storage", ("storage", "backup", "snapshot")),
    ("committed", ("unused commitment",)),
    ("compute", ("compute", "node", "cluster", "instance")),
)


def _classify_redshift_cost_category(charge_description: str | None, sku_id: str | None) -> str:
    """Bucket a Redshift charge below SKU granularity: compute / concurrency-scaling /
    storage / spectrum-scan / other.

    Text-match heuristic over FOCUS-carried fields (mirrors the S3 intelligent-tiering
    signal in ``fetch_efficiency`` above) — AWS FOCUS exports carry no dedicated cost-
    category column, so this reads ChargeDescription/SkuId the same tolerant way.
    """
    return _classify_cost_category(charge_description, sku_id, _REDSHIFT_CATEGORY_RULES)


# ── S3 below-SKU categories ───────────────────────────────────────────────────────
# The S3 twin of _REDSHIFT_CATEGORY_RULES, same first-match-wins precedence, feeding
# the same two consumers (_classify_s3_cost_category in Python, the SQL CASE in
# _cost_subcategory_sql). Exists because Databricks' storage is billed here, not by
# Databricks: "S3 cost" as one number can't tell a storage-growth problem from a
# request-volume one, and Databricks drives heavy LIST/GET metadata traffic, so
# collapsing requests into storage would misdirect every remedy.
#
# The ordering is load-bearing, not cosmetic — each of these would be swallowed by a
# later, broader rule:
#   * early_delete before storage  — "EarlyDelete-ByteHrs" matches storage's "bytehrs"
#   * monitoring    before storage — Intelligent-Tiering's "Monitoring and Automation"
#                                    line mentions storage in its own description
#   * requests      before storage — "Requests-Tier4" reads "Lifecycle Transition
#                                    request", and retrieval SKUs mention storage tiers
# "retrieval" is folded into `requests` deliberately rather than given a sixth bucket:
# AWS's own console groups "Requests & data retrievals", and a retrieval IS a request
# with a per-GB component.
#
# ponytail: every keyword below is UNVERIFIED against a live FOCUS export. The Redshift
# table above needed two corrections after meeting real billing text — "instance" and
# "data scan", without which the single largest line item in the account ($44K) sat
# silently in "other" — so assume this one needs the same and re-check it before
# trusting the split. Open question it depends on: AWS's discriminating *usage type*
# ("TimedStorage-ByteHrs", "Requests-Tier1", "DataTransfer-Out-Bytes") is what these
# keywords really target, and whether it reaches us in SkuId at all in the FOCUS 1.2
# export is unconfirmed. If it doesn't, the fix is an x_UsageType column — which means
# adding to _FOCUS_COLUMNS *and recreating the export* (the FOCUS table version is
# baked into it), so don't design for that up front. This is the same open question as
# the `s3_intelligent_tiering` rule's caveat in efficiency/waste_rules.py, over the
# same text: validating one validates the other.
_S3_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("early_delete", ("earlydelete", "early delete", "early deletion")),
    ("monitoring", ("monitoring", "automation")),
    ("data_transfer", ("datatransfer", "data transfer", "transfer acceleration", "bandwidth")),
    ("requests", ("request", "put, copy", "retrieval", "lifecycle transition", "select")),
    ("storage", ("timedstorage", "bytehrs", "storage", "sizeoverhead")),
)


def _classify_s3_cost_category(charge_description: str | None, sku_id: str | None) -> str:
    """Bucket an S3 charge below SKU granularity: storage / requests / data_transfer /
    monitoring / early_delete / other. The Python twin of the SQL branch in
    :func:`_cost_subcategory_sql`, kept in sync by generating both from
    :data:`_S3_CATEGORY_RULES` (and pinned by a parity test).
    """
    return _classify_cost_category(charge_description, sku_id, _S3_CATEGORY_RULES)


def _classify_cost_category(
    charge_description: str | None,
    sku_id: str | None,
    rules: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    """First matching rule's category over ``ChargeDescription`` + ``SkuId``, else
    ``other`` — the one text-match implementation both service families share."""
    text = f"{charge_description or ''} {sku_id or ''}".lower()
    for category, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return category
    return "other"


def _category_branch(category: str, keywords: tuple[str, ...]) -> str:
    """One ``WHEN … THEN '<category>'`` branch over the shared ``charge_text`` macro."""
    text_expr = "charge_text(nz(ChargeDescription), nz(SkuId))"
    conditions = " OR ".join(f"{text_expr} LIKE '%{keyword}%'" for keyword in keywords)
    return f"WHEN {conditions} THEN '{category}'"


def _category_case(rules: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    """``rules`` as one ``CASE … ELSE 'other' END``, most-specific branch first — the
    SQL port of :func:`_classify_cost_category`, generated from the same table so the
    two can never drift the way lake/seed.py's mapping once drifted from _focus_map's.
    """
    branches = "\n            ".join(_category_branch(c, k) for c, k in rules)
    return f"CASE\n            {branches}\n            ELSE 'other'\n        END"


def _cost_subcategory_sql() -> str:
    """The single ``x_cost_subcategory`` expression this connector splices into
    :func:`flashlight.focus.sql_mapping.mapping_sql` — one CASE with one branch per
    service family, because ``mapping_sql`` accepts exactly one expression and both
    classifiers gate on ServiceName. Evaluated over the raw ``nz(...)`` source columns;
    a row in neither family gets NULL, unchanged.

    ``ChargeCategory = 'Credit'`` is hoisted above both branches because it is NULL
    under either one anyway, so the rule is stated once instead of per family. A credit
    gets NULL rather than a real bucket (so it's excluded from
    ``gold.spend_by_cost_subcategory_month`` entirely — see its ``WHERE
    x_cost_subcategory IS NOT NULL``) because that view is a usage-mix breakdown and a
    credit isn't usage of any kind; text-matching one is also a dead end, since its own
    description ("Acme Corp Goodwill Credits, credit id: …") carries no hint of what it
    was credited against.

    ``NULLIF(…, 'committed')`` stays on the **Redshift branch only**, for the same
    usage-mix reason: an RI/Savings-Plan commitment charge isn't usage either.
    ``committed`` remains a real :data:`_REDSHIFT_CATEGORY_RULES` category that
    :func:`_classify_redshift_cost_category` still returns — it's excluded from this one
    view, not reclassified as 'other', and it's already its own KPI on the Redshift
    page. S3 has no commitment SKUs, so applying the NULLIF globally would buy nothing
    today and would silently swallow a future S3 category named 'committed'.
    """
    return f"""
    CASE
        WHEN nz(ChargeCategory) = 'Credit' THEN NULL
        WHEN coalesce(nz(ServiceName), 'Unknown') IN ({_REDSHIFT_SERVICES_SQL})
            THEN NULLIF(({_category_case(_REDSHIFT_CATEGORY_RULES)}), 'committed')
        WHEN coalesce(nz(ServiceName), 'Unknown') IN ({_S3_SERVICES_SQL})
            THEN {_category_case(_S3_CATEGORY_RULES)}
        ELSE NULL
    END
    """


def _period_in_window(period: str, window: IngestWindow) -> bool:
    """True if billing month ``YYYY-MM`` overlaps the inclusive [start, end] window."""
    year, month = (int(p) for p in period.split("-"))
    month_start = date(year, month, 1)
    month_end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return not (month_end < window.start or month_start > window.end)


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


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
        clauses.append(f'"ServiceName" IN ({_services_sql(frozenset(allowed))})')
    return " AND ".join(clauses)
