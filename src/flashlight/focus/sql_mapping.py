"""FOCUS row -> BRONZE row, expressed as one DuckDB SQL projection.

The vectorized sibling of ``ingest/connectors/_focus_map.py``: instead of pulling
every row through Python + pydantic, DuckDB reads the source (CSV or Parquet, local
or remote) and does all the mapping/coercion in one SQL statement — rename PascalCase
columns, neutralize ``NULL`` sentinels, cast costs to ``DECIMAL(20,6)``, derive
``charge_month``, stamp the ``x_`` extensions, hash the dedupe key — then a single
``COPY ... PARTITION_BY`` writes zstd Parquet. Zero Python per row, constant memory
regardless of source size.

This mirrors the FOCUS rules in ``connectors/_focus_map.py`` set-based (unknown
ChargeCategory -> Usage, unknown ServiceCategory -> Other, ChargeClass only
'Correction', invalid Tags -> ``{}``, any FOCUS column absent from the source is
NULL) so the two never disagree on defaults/fallbacks — the controlled-vocab lists
below are generated from :mod:`flashlight.focus.enums` rather than duplicated as
string literals, and the dedupe key uses the same sha256 algorithm as
:meth:`FocusRecord.dedupe_key`.

Used by ``ingest/connectors/aws_focus.py`` and ``lake/seed.py`` — sources that are
already FOCUS-shaped and DuckDB-scannable. Connectors that build ``FocusRecord``
objects from an API/SDK response (Databricks, Redshift, aws_focus's own Cost
Explorer path) don't use this — DuckDB has nothing to scan there, so the per-row
Python path (``Connector.fetch`` -> the default ``Connector.ingest``, see
``ingest/base.py``) is the correct tool for them.
"""

from __future__ import annotations

import duckdb

from flashlight.focus.enums import (
    ChargeCategory,
    CommitmentDiscountCategory,
    CommitmentDiscountStatus,
    PricingCategory,
    ServiceCategory,
)
from flashlight.focus.model import FOCUS_VERSION

# Every FOCUS column the mapping reads; a column missing from a given source is
# treated as NULL (matches _focus_map.map_focus_row's tolerant `row.get(...)`).
FOCUS_COLUMNS: tuple[str, ...] = (
    "ProviderName", "BillingAccountId", "BillingAccountName", "SubAccountId",
    "SubAccountName", "BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart",
    "ChargePeriodEnd", "BillingCurrency", "BilledCost", "EffectiveCost", "ListCost",
    "ContractedCost", "ChargeCategory", "ChargeClass", "ChargeDescription",
    "ServiceCategory", "ServiceName", "SkuId", "RegionId", "PricingCategory",
    "ResourceId", "ResourceName", "ResourceType", "ConsumedQuantity", "ConsumedUnit", "Tags",
    "CommitmentDiscountId", "CommitmentDiscountType", "CommitmentDiscountCategory",
    "CommitmentDiscountName", "CommitmentDiscountStatus", "CommitmentDiscountQuantity",
    "CommitmentDiscountUnit", "InvoiceId", "InvoiceIssuerName",
)

# Controlled vocab, generated from the enums (not hand-copied) so this can't drift
# from focus/enums.py the way a second hard-coded literal list eventually would.
_CHARGE_CATEGORIES_SQL = ", ".join(f"'{c.value}'" for c in ChargeCategory)
_SERVICE_CATEGORIES_SQL = ", ".join(f"'{c.value}'" for c in ServiceCategory)
_COMMITMENT_CATEGORIES_SQL = ", ".join(f"'{c.value}'" for c in CommitmentDiscountCategory)
_COMMITMENT_STATUSES_SQL = ", ".join(f"'{c.value}'" for c in CommitmentDiscountStatus)
_PRICING_CATEGORIES_SQL = ", ".join(f"'{c.value}'" for c in PricingCategory)

# Passthrough identity columns a connector's FOCUS-shaped source may additionally
# carry beyond the FOCUS spec itself (Databricks: the physical usage record's id +
# its correction type, so RETRACTION/RESTATEMENT rows stay distinct — see
# FocusRecord.dedupe_key). Absent from a source -> NULL, same as any FOCUS_COLUMNS
# entry; present -> flows into x_record_id/x_record_type and the dedupe key below.
_PASSTHROUGH_COLUMNS: tuple[str, ...] = ("x_RecordId", "x_RecordType")

# DuckDB types that need to_json() rather than a plain VARCHAR cast to stringify —
# a nested/structured value (only Tags, delivered as MAP(VARCHAR, VARCHAR) by AWS
# FOCUS Parquet, hits this in practice) would otherwise render as DuckDB's own
# `{k=v, ...}` literal syntax instead of JSON.
_COMPLEX_TYPE_MARKERS = ("MAP(", "STRUCT(", "UNION(")


def ensure_helpers(con: duckdb.DuckDBPyConnection) -> None:
    """Install the session state every mapping query below depends on. Idempotent.

    ``TimeZone='UTC'`` matters even though every timestamp is immediately
    re-parsed as ``TIMESTAMPTZ``: DuckDB stringifies a native TIMESTAMPTZ column
    using the *session* timezone, so without pinning it to UTC first, a Parquet
    source's embedded (possibly non-UTC) offset would render as whatever the
    host machine's local timezone happens to be — silently wrong, and dependent
    on where the process runs. Pinning first makes the stringify step
    deterministic; the values still carry their real offset in the text either
    way, so the later ``TIMESTAMPTZ`` cast resolves the correct instant
    regardless.
    """
    con.execute("SET TimeZone='UTC'")
    con.execute(
        "CREATE OR REPLACE MACRO nz(x) AS "
        "CASE WHEN x IS NULL OR upper(trim(x)) IN ('', 'NULL', 'NONE', 'NAN') "
        "THEN NULL ELSE trim(x) END"
    )


def present_columns(con: duckdb.DuckDBPyConnection, source_sql: str) -> dict[str, str]:
    """Column name -> DuckDB type name, for whatever columns exist in ``source_sql``.

    ``source_sql`` must be a valid FROM-clause item as-is — a bare table-producing
    expression (``read_csv(...)``, ``read_parquet(...)``) or an already-parenthesized
    subquery (``(SELECT ... WHERE ...)``); this doesn't add its own parens around it.
    """
    rows = con.execute(f"DESCRIBE SELECT * FROM {source_sql}").fetchall()
    return {row[0]: row[1] for row in rows}


def _stringify(column: str, duck_type: str) -> str:
    """One FOCUS column, as VARCHAR — ``to_json`` for a structured type, else CAST."""
    upper = duck_type.upper()
    if upper.endswith("[]") or upper.startswith(_COMPLEX_TYPE_MARKERS):
        return f'to_json({column})::VARCHAR AS "{column}"'
    return f'CAST({column} AS VARCHAR) AS "{column}"'


def mapping_sql(
    source_sql: str,
    *,
    connector: str,
    run_id: str,
    focus_version: str = FOCUS_VERSION,
    present: dict[str, str],
    cost_subcategory_sql: str = "CAST(NULL AS VARCHAR)",
    compute_class_sql: str = "'n/a'",
    effective_is_list: bool = False,
) -> str:
    """The FOCUS-shaped ``source_sql`` relation -> one BRONZE-shaped ``SELECT``.

    ``source_sql`` must be a valid FROM-clause item as-is (see
    :func:`present_columns`). ``present`` (from :func:`present_columns`) drives
    both which FOCUS columns exist in the source (absent -> NULL) and how each
    is stringified (plain scalar vs. structured/JSON) before the shared
    coercion rules run — the same rules as ``connectors/_focus_map.py``, so a
    row is mapped identically whether it's read through this SQL path or the
    Python one.

    ``cost_subcategory_sql`` is spliced in as ``x_cost_subcategory``'s
    expression, evaluated over the *raw* ``nz(...)`` source columns (not the
    mapped output — a SELECT list can't reference its own sibling aliases);
    pass a CASE expression referencing ``ServiceName``/``ChargeDescription``/
    ``SkuId`` for a connector-specific classifier (see aws_focus.py's Redshift
    rule), or leave the default NULL. ``compute_class_sql`` is the same idea for
    ``x_compute_class`` (see databricks.py's SKU-based classifier).
    ``effective_is_list`` stamps ``x_effective_is_list`` — a connector-wide
    constant, not a per-row expression.

    ``x_record_id``/``x_record_type`` come from the source's ``x_RecordId``/
    ``x_RecordType`` columns when present (NULL otherwise, like any FOCUS
    column) and feed the dedupe key's trailing slots — see
    ``_PASSTHROUGH_COLUMNS``.

    Rows sharing a ``dedupe_key`` collapse to one via ``QUALIFY`` (first
    surviving row is arbitrary — a real collision means the identical physical
    row appeared twice, so which copy wins is immaterial, exactly as
    ``lake/bronze.dedupe`` documents for the row-based path).
    """
    src_cols = ", ".join(
        _stringify(c, present[c]) if c in present else f'NULL AS "{c}"'
        for c in FOCUS_COLUMNS + _PASSTHROUGH_COLUMNS
    )
    # Hash every source column, not a curated subset of "identifying" dimensions —
    # a curated subset silently conflates genuinely distinct charges that happen to
    # share every dimension it tracks but differ in one it doesn't. Confirmed against
    # a real AWS export: multiple Reserved Instance purchases in the same period/SKU
    # (AWS leaves ResourceId null on a purchase — it isn't tied to one resource) all
    # shared the old key and collapsed to one, silently dropping the others. Two rows
    # now collapse only when identical in every column, the only case that's actually
    # "the same physical row appeared twice." Columns are already stringified (JSON
    # for a MAP/STRUCT like Tags) by `src` above, so a plain coalesce/nz is enough —
    # no per-column casting needed here.
    _dedupe_cols_sql = ", ".join(
        f'coalesce(nz("{c}"), \'\')' for c in FOCUS_COLUMNS + _PASSTHROUGH_COLUMNS
    )
    effective_is_list_sql = "true" if effective_is_list else "false"
    return f"""
    WITH raw AS (
        SELECT * FROM {source_sql}
    ),
    src AS (
        SELECT {src_cols} FROM raw
    ),
    mapped AS (
        SELECT
            sha256(concat_ws('|', '{connector}', {_dedupe_cols_sql}))
                                                                    AS dedupe_key,
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
            try_cast(nz(ChargePeriodStart) AS TIMESTAMPTZ)          AS charge_period_start,
            coalesce(try_cast(nz(ChargePeriodEnd) AS TIMESTAMPTZ),
                     try_cast(nz(ChargePeriodStart) AS TIMESTAMPTZ)) AS charge_period_end,
            coalesce(nz(BillingCurrency), 'USD')                    AS billing_currency,
            coalesce(try_cast(nz(BilledCost) AS DECIMAL(20, 6)), 0)      AS billed_cost,
            coalesce(try_cast(nz(EffectiveCost) AS DECIMAL(20, 6)), 0)   AS effective_cost,
            coalesce(try_cast(nz(ListCost) AS DECIMAL(20, 6)), 0)        AS list_cost,
            coalesce(try_cast(nz(ContractedCost) AS DECIMAL(20, 6)), 0)  AS contracted_cost,
            CASE WHEN nz(ChargeCategory) IN ({_CHARGE_CATEGORIES_SQL})
                 THEN nz(ChargeCategory) ELSE 'Usage' END           AS charge_category,
            CASE WHEN nz(ChargeClass) = 'Correction' THEN 'Correction' ELSE NULL END
                                                                    AS charge_class,
            nz(ChargeDescription)                                   AS charge_description,
            CASE WHEN nz(ServiceCategory) IN ({_SERVICE_CATEGORIES_SQL})
                 THEN nz(ServiceCategory) ELSE 'Other' END          AS service_category,
            coalesce(nz(ServiceName), 'Unknown')                    AS service_name,
            nz(SkuId)                                               AS sku_id,
            nz(RegionId)                                            AS region_id,
            CASE WHEN nz(PricingCategory) IN ({_PRICING_CATEGORIES_SQL})
                 THEN nz(PricingCategory) ELSE NULL END              AS pricing_category,
            nz(ResourceId)                                          AS resource_id,
            nz(ResourceName)                                        AS resource_name,
            nz(ResourceType)                                        AS resource_type,
            try_cast(nz(ConsumedQuantity) AS DOUBLE)                AS consumed_quantity,
            nz(ConsumedUnit)                                        AS consumed_unit,
            CASE WHEN try_cast(nz(Tags) AS JSON) IS NULL THEN '{{}}' ELSE nz(Tags) END
                                                                    AS tags,
            nz(CommitmentDiscountId)                                AS commitment_discount_id,
            nz(CommitmentDiscountType)                              AS commitment_discount_type,
            CASE WHEN nz(CommitmentDiscountCategory) IN ({_COMMITMENT_CATEGORIES_SQL})
                 THEN nz(CommitmentDiscountCategory) ELSE NULL END  AS commitment_discount_category,
            nz(CommitmentDiscountName)                              AS commitment_discount_name,
            CASE WHEN nz(CommitmentDiscountStatus) IN ({_COMMITMENT_STATUSES_SQL})
                 THEN nz(CommitmentDiscountStatus) ELSE NULL END    AS commitment_discount_status,
            try_cast(nz(CommitmentDiscountQuantity) AS DOUBLE)      AS commitment_discount_quantity,
            nz(CommitmentDiscountUnit)                              AS commitment_discount_unit,
            nz(InvoiceId)                                           AS invoice_id,
            nz(InvoiceIssuerName)                                   AS invoice_issuer_name,
            {compute_class_sql}                                     AS x_compute_class,
            '{focus_version}'                                       AS x_focus_version,
            {effective_is_list_sql}                                 AS x_effective_is_list,
            nz(x_RecordId)                                          AS x_record_id,
            nz(x_RecordType)                                        AS x_record_type,
            {cost_subcategory_sql}                                  AS x_cost_subcategory,
            '{connector}'                                           AS x_source_connector,
            strftime(try_cast(nz(ChargePeriodStart) AS TIMESTAMPTZ), '%Y-%m') AS charge_month
        FROM src
        WHERE nz(ChargePeriodStart) IS NOT NULL
    )
    SELECT * FROM mapped
    QUALIFY row_number() OVER (PARTITION BY dedupe_key) = 1
    """


def assert_single_currency(
    con: duckdb.DuckDBPyConnection, relation: str, *, connector: str, base_currency: str
) -> None:
    """Raise :class:`FocusValidationError` if ``relation`` carries more than one
    billing currency (mixed-currency sums are unsafe) — the same guard every
    connector applies, whichever path (row-based or vectorized) it uses."""
    from flashlight.core.exceptions import FocusValidationError

    bad = [
        row[0]
        for row in con.execute(
            f"SELECT DISTINCT billing_currency FROM {relation} "  # noqa: S608
            "WHERE billing_currency != ?",
            [base_currency],
        ).fetchall()
    ]
    if bad:
        raise FocusValidationError(
            f"{connector}: currencies {sorted(bad)} != base {base_currency}; "
            "mixed-currency sums are unsafe"
        )
