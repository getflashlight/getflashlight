# FOCUS and cost integrity

Flashlight normalizes source billing into a canonical `FocusRecord`, aligned to the
[FinOps FOCUS specification](https://focus.finops.org/focus-specification/). The goal is
not merely to display source exports: it is to produce additive, comparable cost metrics
with their assumptions visible.

## The canonical cost

Flashlight uses `EffectiveCost` as the canonical cost measure in its cost views. A view
does not mix cost columns. This prevents a chart from silently combining billed, list,
contracted, or amortized-style amounts.

| Rule | Why it matters |
| --- | --- |
| One named cost measure per view | Totals retain a clear definition |
| Charge-period grain | Cost is additive across time, accounts, and resources |
| Credits and refunds keep their sign | Net cost is not overstated |
| One currency is asserted at ingest | Totals never silently add different currencies |
| Partial current periods are identified | A month-to-date value is not mistaken for a full month |
| Unattributed AWS cost remains visible | Missing allocation is a result, not hidden data loss |

## FOCUS record

Each BRONZE charge record contains provider and account identity, billing and charge
periods, cost columns, service/SKU/resource dimensions, usage, commitment context, tags,
and source provenance. Provider-specific facts are preserved as `x_` extensions rather
than treated as universal FOCUS fields.

The detailed field-level contract is in [Data contracts](../reference/data-contracts.md).

## Reconciliation

To reconcile Flashlight with a provider statement:

1. Match the same charge/billing window and currency.
2. Compare the same cost basis; Flashlight cost charts use `EffectiveCost`.
3. Include signed credits, refunds, adjustments, and tax only when they are present in
   the source and in the comparison statement.
4. Check whether the latest period is partial.
5. Inspect connector scope and filters; an excluded service or account is deliberately
   absent, not a dashboard error.

## Recoverable spend is separate

Efficiency records are not another billing source. They contain utilization and activity
signals, then GOLD rules calculate candidate recoverable spend from them. This keeps the
answer to “what did we spend?” separate from “what part looks avoidable?” Read
[Efficiency / waste](../design/efficiency-waste.md) for the method and limitations.
