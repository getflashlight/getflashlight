"""AI serving-usage record + its Parquet schema — the token/request measurement plane.

A third telemetry dataset alongside the efficiency/waste and driver-health planes: same
*pattern* (aggregated-at-source record → partition-replace Parquet → DuckDB view → GOLD
view), a different table because it doesn't fit either existing contract.

Why not :class:`~flashlight.efficiency.model.EfficiencyRecord`:

* It carries exactly one ``native_quantity``/``native_unit`` pair, and for an endpoint that
  pair must be DBUs — that's what reconciles to the FOCUS bill. Input tokens and output
  tokens are two quantities at two prices; neither can displace DBUs, and putting both in
  ``cause_detail`` makes them JSON strings, un-aggregatable as GOLD measures and outside
  ``MEASURE_UNITS`` entirely.
* Its grain is (entity × month) where the entity is the *actionable* unit. The token fact is
  (endpoint × served model × requester × request-project × month) — a four-way fan-out.
  Faking it with a composite ``entity_id`` would make ``waste_by_owner_month`` rank one
  endpoint many times and break ``waste_resolution_month``, which re-detects on ``entity_id``.
* 40M tokens is not waste. ``waste_record`` only holds entities a rule *fired* on; a token
  volume table carries no verdict.

The endpoint *verdicts* (idle provisioned capacity, failed-request spend) still go through
the efficiency plane as ``entity_type='endpoint'`` rows, so the recoverable-dollar story
stays on one surface. This plane is the measurement; that one is the judgement.

**There is deliberately no cost column here.** The endpoint is a FOCUS ``ResourceId``, so
its spend is already canonical in the FOCUS plane (``gold.ai_spend_month``). A second,
``list_prices``-derived dollar figure on this record would be a second source of truth for
AI spend. The cost↔token join happens once, in GOLD.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
from pydantic import BaseModel, field_validator

#: The serving modes we distinguish, because they bill differently and therefore permit
#: different cost claims. ``pay_per_token`` is metered per token, so splitting an endpoint's
#: charge by token share is a proportional split of a per-token charge.
#: ``provisioned_throughput``/``provisioned_compute`` bill by the provisioned hour — an idle
#: provisioned endpoint bills real money with zero tokens, so a token-share split would
#: silently move its cost onto whoever did send tokens. ``external`` means Databricks bills
#: the gateway hop while the model vendor bills the tokens, so Databricks $ is not the token
#: cost at all. ``unknown`` is the honest fallback and never carries a $/token claim.
SERVING_MODES: tuple[str, ...] = (
    "pay_per_token",
    "provisioned_throughput",
    "provisioned_compute",
    "external",
    "unknown",
)


class AiUsageRecord(BaseModel):
    """One (endpoint × served entity × requester × request-project)'s token volume for one
    month, aggregated at source. No dollar figure — see the module docstring."""

    provider_name: str  # Databricks | … (partition key)
    charge_month: date  # first of month (partition key)

    # ── Identity ────────────────────────────────────────────────────────────
    # endpoint_id is the FOCUS ResourceId for the endpoint-shaped AI products, which is
    # what makes the GOLD cost join possible (databricks_focus_1_3.sql maps
    # usage_metadata.endpoint_id into ResourceId for MODEL_SERVING and friends).
    endpoint_id: str
    endpoint_name: str | None = None
    served_entity_id: str | None = None
    model_name: str | None = None  # served_entities.entity_name
    model_version: str | None = None
    model_kind: str | None = None  # FOUNDATION_MODEL | CUSTOM_MODEL | EXTERNAL_MODEL | …
    serving_mode: str = "unknown"  # one of SERVING_MODES — drives what cost claim is allowed

    # ── Attribution ─────────────────────────────────────────────────────────
    requester: str | None = None  # the identity that issued the request
    # Request-level project, from the client-supplied usage_context map. Usually absent —
    # the endpoint's own cost-allocation tag (already in BRONZE) is the high-coverage
    # source, and GOLD coalesces the two so this one wins when present.
    usage_context_project: str | None = None

    # ── Config facts (from served_entities) ─────────────────────────────────
    # Not measurements — they feed the endpoint EfficiencyRecord's cause_detail so the
    # scale-to-zero / oversized-workload rules have something to gate on.
    scale_to_zero_enabled: bool | None = None
    workload_size: str | None = None
    workload_type: str | None = None  # CPU | GPU_SMALL | GPU_MEDIUM | GPU_LARGE
    min_provisioned_throughput: float | None = None
    max_provisioned_throughput: float | None = None

    # ── Measures ────────────────────────────────────────────────────────────
    request_count: int = 0
    error_request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Tokens burned on a request that errored — spend with no result. Split in/out because
    # they price differently wherever they price at all.
    error_input_tokens: int = 0
    error_output_tokens: int = 0
    # Rides along for cause_detail; deliberately NOT a GOLD measure — MEASURE_UNITS has no
    # unit for elapsed time, and inventing one for a single column isn't worth it.
    total_duration_ms: int = 0

    x_source_connector: str = "unknown"

    @field_validator("charge_month")
    @classmethod
    def _first_of_month(cls, v: date) -> date:
        return v.replace(day=1)

    @field_validator("serving_mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        """An unrecognized mode becomes 'unknown' rather than flowing through.

        A typo'd or newly-invented mode string must not silently reach GOLD, where the
        cost_allocation_basis CASE would fall through to 'unknown' anyway — better to
        normalize here so the record itself never claims a mode we don't understand.
        """
        return v if v in SERVING_MODES else "unknown"


#: Non-partition columns first, then the two partition keys last — mirrors
#: lake/driver_health_schema.py::DRIVER_HEALTH_SCHEMA and lake/metrics_schema.py.
AI_USAGE_SCHEMA: pa.Schema = pa.schema(
    [
        ("endpoint_id", pa.string()),
        ("endpoint_name", pa.string()),
        ("served_entity_id", pa.string()),
        ("model_name", pa.string()),
        ("model_version", pa.string()),
        ("model_kind", pa.string()),
        ("serving_mode", pa.string()),
        ("requester", pa.string()),
        ("usage_context_project", pa.string()),
        ("scale_to_zero_enabled", pa.bool_()),
        ("workload_size", pa.string()),
        ("workload_type", pa.string()),
        ("min_provisioned_throughput", pa.float64()),
        ("max_provisioned_throughput", pa.float64()),
        ("request_count", pa.int64()),
        ("error_request_count", pa.int64()),
        ("input_tokens", pa.int64()),
        ("output_tokens", pa.int64()),
        ("error_input_tokens", pa.int64()),
        ("error_output_tokens", pa.int64()),
        ("total_duration_ms", pa.int64()),
        ("x_source_connector", pa.string()),
        # ── Hive partition keys (written as dirs, restored on read) ──────────
        ("provider_name", pa.string()),
        ("charge_month", pa.string()),  # YYYY-MM
    ]
)

PARTITION_COLUMNS: tuple[str, ...] = ("provider_name", "charge_month")


def charge_month_of(record: AiUsageRecord) -> str:
    """Partition key for a record: the charge month, ``YYYY-MM``."""
    return record.charge_month.strftime("%Y-%m")


def record_to_row(record: AiUsageRecord) -> dict[str, object]:
    """Flatten an :class:`AiUsageRecord` into an ai-usage row dict."""
    return {
        "endpoint_id": record.endpoint_id,
        "endpoint_name": record.endpoint_name,
        "served_entity_id": record.served_entity_id,
        "model_name": record.model_name,
        "model_version": record.model_version,
        "model_kind": record.model_kind,
        "serving_mode": record.serving_mode,
        "requester": record.requester,
        "usage_context_project": record.usage_context_project,
        "scale_to_zero_enabled": record.scale_to_zero_enabled,
        "workload_size": record.workload_size,
        "workload_type": record.workload_type,
        "min_provisioned_throughput": record.min_provisioned_throughput,
        "max_provisioned_throughput": record.max_provisioned_throughput,
        "request_count": record.request_count,
        "error_request_count": record.error_request_count,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "error_input_tokens": record.error_input_tokens,
        "error_output_tokens": record.error_output_tokens,
        "total_duration_ms": record.total_duration_ms,
        "x_source_connector": record.x_source_connector,
        "provider_name": str(record.provider_name),
        "charge_month": charge_month_of(record),
    }


def build_table(records: list[AiUsageRecord]) -> pa.Table:
    """Build a typed Arrow table (``AI_USAGE_SCHEMA``) from validated records."""
    return pa.Table.from_pylist([record_to_row(r) for r in records], schema=AI_USAGE_SCHEMA)


def empty_table() -> pa.Table:
    """An empty, fully-typed ai-usage table — the no-data fallback for readers."""
    return AI_USAGE_SCHEMA.empty_table()
