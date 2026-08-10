# Anonymized production-shape profile for the demo

This document is an **aggregate-only** profile of the local production GOLD layer.
It exists to make `flashlight sample` behave like a smaller, safe facsimile of a
real account. It contains no resource names, account identifiers, user identities,
URLs, invoice identifiers, raw tags, or copied FOCUS/GOLD records.

## Databricks shape to reproduce

The production footprint contains a large JOBS population, hundreds of interactive
and notebook workloads, a smaller set of SQL warehouses, hundreds of warehouse-user
attribution rows, and a compact endpoint fleet. Its visible service mix also includes
JOBS, INTERACTIVE, ALL_PURPOSE, SQL, MODEL_SERVING, DATABASE, APPS, AI_GATEWAY,
ONLINE_TABLES, LAKEBASE, GENIE, NETWORKING, and PREDICTIVE_OPTIMIZATION.

The demo target is deliberately scaled down while retaining that shape:

| Demo entity family | Target population | Purpose |
|---|---:|---|
| Jobs | 60–80 | Varied pipeline owners, schedules, runtimes, utilization, retries |
| Interactive / all-purpose clusters | 25–35 | Policy and EC2 pricing-model drill-through |
| Notebooks | 30–45 | Job-migration and usage-pattern findings |
| SQL warehouses | 5–8 | Tagging, auto-stop, cache/frequency findings |
| SQL warehouse users | 45–70 | Concentration and ownership drill-through; allocated, not billed twice |
| Model-serving endpoints | 5–8 | Model/token/requester allocation and endpoint policy |

Databricks waste must cover, where source facts permit: failed runs, low utilization,
autoscale misconfiguration, oversized nodes, Photon suitability, on-demand-only
pricing, warehouse frequency/cache patterns, warehouse-user concentration, and
notebook-to-job opportunities. Every generated billed entity must still reconcile to
one FOCUS resource/month; derived user/notebook rows carry allocation provenance and
must never create a second provider bill.

## Redshift shape to reproduce

The production footprint has a small billed-cluster fleet, with a much larger layer of
query-pattern, table, storage, and warehouse-user facts. The sample intentionally
keeps the user-requested two billed Redshift clusters, then uses anonymized derived
telemetry beneath them:

| Demo entity family | Target population | Purpose |
|---|---:|---|
| Billed Redshift clusters | 2 | AWS ARN-shaped cost, policies, and invoice drill-through |
| Query patterns | 50–80 | Spill, skew, queue-wait, and Spectrum findings |
| Tables | 180–250 | Stale maintenance, compression, and Spectrum-table findings |
| Storage objects | 75–125 | Intelligent-tiering opportunities |
| Warehouse users | 20–35 | Concentration attribution without double billing |

Redshift waste must include Spectrum scan/table patterns, query spill/skew, stale
maintenance/compression, queue wait, storage tiering, and reserved-instance coverage.
Unpriced diagnostic findings remain explicitly `$0`; priced findings reconcile to the
two-cluster provider cost and are not a new cost source.

## Sanitization contract

Replace the following values deterministically before any production-shaped profile
is used by the demo: account and invoice IDs, resource IDs/names, ARNs, emails,
owners, projects, teams, application names, endpoint/model names, S3 bucket URLs,
credential names, policy IDs, and all tag values. Preserve only: schemas, view names,
column types, row grains, cardinalities, category distributions, date coverage, and
reconciliation relationships.
