# Build a connector

Flashlight's extension boundary is a canonical data contract, not a dashboard fork. A
new source maps into `FocusRecord` for cost and may optionally implement the telemetry
record contracts that its platform can support.

## Connector contract

Implement a stateless subclass of `flashlight.ingest.base.Connector`. A connector may:

- yield `FocusRecord` values from `fetch()` for row-oriented sources;
- implement a vectorized ingest path when the source is already FOCUS-shaped and
  DuckDB-scannable;
- yield `EfficiencyRecord` values for utilization/activity signals;
- yield `DriverHealthRecord`, `AiUsageRecord`, `StorageLocationRecord`, or
  `ComputeInstanceRecord` where their semantics genuinely apply.

Do not force provider-specific facts into universal fields. Preserve them in an `x_`
extension or structured detail field, and add a GOLD rule/view only after its grain and
reconciliation behavior are defined.

## Design requirements

1. Every cost row must have provider/account identity, charge periods, currency, and
   `EffectiveCost`.
2. A connector must replace the window it owns predictably, so retries are idempotent.
3. Errors must identify the connection and source condition without exposing secrets.
4. Telemetry failures should not destroy successful canonical cost ingestion.
5. Add tests for validation, mapping, deduplication, window replacement, and published
   GOLD results.

## Registering a connector

Add the Pydantic configuration model to the connector configuration union, register it
with the ingest runner, add a thoroughly commented example, and document required
permissions and limitations in the connector support matrix. Treat these as one change:
the public configuration schema is part of the connector's API.

## Before proposing a new metric

Write down its input contract, grain, measure unit, cost basis, period semantics,
attribution behavior, and degradation behavior when source data is absent. The existing
[design decisions](../design/efficiency-waste.md) are examples of that level of rigor.
