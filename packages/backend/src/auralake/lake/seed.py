"""Vectorized FOCUS-CSV → BRONZE Parquet loader.

The optimal path for a file that's *already* FOCUS-shaped: instead of pulling
every row through Python + pydantic (the connector path), DuckDB reads the CSV and
does all the mapping/coercion in one SQL projection — rename PascalCase → snake,
neutralize ``NULL`` sentinels, cast costs to ``DECIMAL(20,6)``, derive
``charge_month``, stamp the ``x_`` extensions — then ``COPY … PARTITION_BY`` writes
zstd Parquet. Zero Python per row, fully vectorized, constant memory.

This mirrors the FOCUS rules in ``connectors/_focus_map.py`` set-based: unknown
ChargeCategory → Usage, unknown ServiceCategory → Other, ChargeClass only
'Correction', invalid Tags → ``{}``, and any FOCUS column absent from the file is
treated as NULL (the connector uses ``row.get`` for the same tolerance). Currency
is asserted single == base, exactly like the connector path. Used by ``auralake
sample``; the same shape can back ``focus_file``/``aws_focus`` for bulk loads.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from auralake.core.exceptions import FocusValidationError
from auralake.core.logging import get_logger
from auralake.core.settings import get_settings
from auralake.lake import bronze, duck, paths

logger = get_logger(__name__)

# Every FOCUS column the mapping reads; missing ones are filled with NULL in `src`.
_FOCUS_COLUMNS = (
    "ProviderName", "BillingAccountId", "BillingAccountName", "SubAccountId",
    "SubAccountName", "BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart",
    "ChargePeriodEnd", "BillingCurrency", "BilledCost", "EffectiveCost", "ListCost",
    "ContractedCost", "ChargeCategory", "ChargeClass", "ChargeDescription",
    "ServiceCategory", "ServiceName", "SkuId", "RegionId", "ResourceId",
    "ResourceName", "ResourceType", "ConsumedQuantity", "ConsumedUnit", "Tags",
)

# FOCUS controlled vocab, mirrored from focus/enums.py for the set-based fallbacks.
_CHARGE_CATEGORIES = "'Usage','Purchase','Tax','Credit','Adjustment'"
_SERVICE_CATEGORIES = (
    "'AI and Machine Learning','Analytics','Compute','Databases','Developer Tools',"
    "'Management and Governance','Networking','Security, Identity, and Compliance',"
    "'Storage','Other'"
)


def _present_columns(con: duckdb.DuckDBPyConnection, csv_path: str) -> set[str]:
    con.execute(f"SELECT * FROM read_csv('{csv_path}', header=true, all_varchar=true) LIMIT 0")
    return {col[0] for col in con.description}


def _mapping_sql(csv_path: str, connector: str, run_id: str, present: set[str]) -> str:
    """The single SQL projection: FOCUS CSV columns → BRONZE columns.

    ``src`` first normalizes the column set so every FOCUS column exists (absent →
    NULL); ``mapped`` then coerces them. Splitting it keeps the coercion logic
    independent of which columns a given export happens to include.
    """
    path = csv_path.replace("'", "''")
    src_cols = ", ".join(
        f'{c if c in present else "NULL"} AS "{c}"' for c in _FOCUS_COLUMNS
    )
    return f"""
    CREATE OR REPLACE MACRO nz(x) AS
        CASE WHEN x IS NULL OR upper(trim(x)) IN ('', 'NULL', 'NONE', 'NAN')
             THEN NULL ELSE trim(x) END;

    CREATE OR REPLACE TEMP VIEW _seed AS
    WITH raw AS (
        SELECT * FROM read_csv('{path}', header = true, all_varchar = true)
    ),
    src AS (
        SELECT {src_cols} FROM raw
    ),
    mapped AS (
        SELECT
            md5(concat_ws('|',
                coalesce(nz(ProviderName), 'Unknown'),
                coalesce(nz(BillingAccountId), 'unknown'),
                coalesce(nz(SubAccountId), ''),
                CAST(try_cast(nz(ChargePeriodStart) AS TIMESTAMP) AS VARCHAR),
                CAST(coalesce(try_cast(nz(ChargePeriodEnd) AS TIMESTAMP),
                              try_cast(nz(ChargePeriodStart) AS TIMESTAMP)) AS VARCHAR),
                coalesce(nz(ServiceName), 'Unknown'),
                coalesce(nz(SkuId), ''),
                coalesce(nz(ResourceId), ''),
                CASE WHEN nz(ChargeCategory) IN ({_CHARGE_CATEGORIES})
                     THEN nz(ChargeCategory) ELSE 'Usage' END,
                CASE WHEN nz(ChargeClass) = 'Correction' THEN 'Correction' ELSE '' END,
                '{connector}', '', ''
            ))                                                       AS dedupe_key,
            '{run_id}'                                              AS ingest_run_id,
            now()                                                   AS x_ingested_at,
            coalesce(nz(ProviderName), 'Unknown')                   AS provider_name,
            coalesce(nz(BillingAccountId), 'unknown')               AS billing_account_id,
            nz(BillingAccountName)                                  AS billing_account_name,
            nz(SubAccountId)                                        AS sub_account_id,
            nz(SubAccountName)                                      AS sub_account_name,
            coalesce(try_cast(nz(BillingPeriodStart) AS DATE),
                     try_cast(nz(ChargePeriodStart) AS DATE))       AS billing_period_start,
            coalesce(try_cast(nz(BillingPeriodEnd) AS DATE),
                     try_cast(nz(ChargePeriodEnd) AS DATE),
                     try_cast(nz(ChargePeriodStart) AS DATE))       AS billing_period_end,
            try_cast(nz(ChargePeriodStart) AS TIMESTAMP) AT TIME ZONE 'UTC'
                                                                    AS charge_period_start,
            coalesce(try_cast(nz(ChargePeriodEnd) AS TIMESTAMP),
                     try_cast(nz(ChargePeriodStart) AS TIMESTAMP)) AT TIME ZONE 'UTC'
                                                                    AS charge_period_end,
            coalesce(nz(BillingCurrency), 'USD')                    AS billing_currency,
            coalesce(try_cast(nz(BilledCost) AS DECIMAL(20, 6)), 0)      AS billed_cost,
            coalesce(try_cast(nz(EffectiveCost) AS DECIMAL(20, 6)), 0)   AS effective_cost,
            coalesce(try_cast(nz(ListCost) AS DECIMAL(20, 6)), 0)        AS list_cost,
            coalesce(try_cast(nz(ContractedCost) AS DECIMAL(20, 6)), 0)  AS contracted_cost,
            CASE WHEN nz(ChargeCategory) IN ({_CHARGE_CATEGORIES})
                 THEN nz(ChargeCategory) ELSE 'Usage' END           AS charge_category,
            CASE WHEN nz(ChargeClass) = 'Correction' THEN 'Correction' ELSE NULL END
                                                                    AS charge_class,
            nz(ChargeDescription)                                   AS charge_description,
            CASE WHEN nz(ServiceCategory) IN ({_SERVICE_CATEGORIES})
                 THEN nz(ServiceCategory) ELSE 'Other' END          AS service_category,
            coalesce(nz(ServiceName), 'Unknown')                    AS service_name,
            nz(SkuId)                                               AS sku_id,
            nz(RegionId)                                            AS region_id,
            nz(ResourceId)                                          AS resource_id,
            nz(ResourceName)                                        AS resource_name,
            nz(ResourceType)                                        AS resource_type,
            try_cast(nz(ConsumedQuantity) AS DOUBLE)                AS consumed_quantity,
            nz(ConsumedUnit)                                        AS consumed_unit,
            CASE WHEN try_cast(nz(Tags) AS JSON) IS NULL THEN '{{}}' ELSE nz(Tags) END
                                                                    AS tags,
            'n/a'                                                   AS x_compute_class,
            '1.1'                                                   AS x_focus_version,
            false                                                   AS x_effective_is_list,
            CAST(NULL AS VARCHAR)                                   AS x_record_id,
            CAST(NULL AS VARCHAR)                                   AS x_record_type,
            '{connector}'                                           AS x_source_connector,
            strftime(try_cast(nz(ChargePeriodStart) AS TIMESTAMP), '%Y-%m') AS charge_month
        FROM src
        WHERE nz(ChargePeriodStart) IS NOT NULL
    )
    SELECT * FROM mapped
    QUALIFY row_number() OVER (PARTITION BY dedupe_key) = 1;
    """


def seed_from_csv(csv_path: Path, *, connector: str, ingest_run_id: str) -> int:
    """Load a FOCUS CSV into BRONZE set-based (full replace for *connector*). Returns rows.

    Asserts a single billing currency matching ``AURALAKE_BASE_CURRENCY`` — the same
    mixed-currency guard the connector path enforces.
    """
    settings = get_settings()
    path = str(csv_path).replace("'", "''")
    con = duck.connect()
    try:
        present = _present_columns(con, path)
        con.execute(_mapping_sql(str(csv_path), connector, ingest_run_id, present))

        currencies = [
            row[0]
            for row in con.execute("SELECT DISTINCT billing_currency FROM _seed").fetchall()
        ]
        bad = [c for c in currencies if c != settings.base_currency]
        if bad:
            raise FocusValidationError(
                f"{connector}: currencies {bad} != base {settings.base_currency}; "
                "mixed-currency sums are unsafe"
            )

        result = con.execute("SELECT count(*) FROM _seed").fetchone()
        count = int(result[0]) if result else 0
        if count == 0:
            logger.info("seed_empty", connector=connector)
            return 0

        bronze.purge_connector(connector)
        paths.bronze_dir().mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY _seed TO '{paths.bronze_dir()}' ({bronze.copy_options()})"  # noqa: S608
        )
    finally:
        con.close()
    logger.info("seed_written", connector=connector, rows=count)
    return count
