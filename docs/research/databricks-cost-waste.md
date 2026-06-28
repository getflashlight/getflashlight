# Research: Databricks cost waste & efficiency ("shadow waste")

> Compiled 2026-06-28 from a fan-out web-research run (FinOps Foundation, Databricks
> official docs, and vendor sources — Unravel, CloudZero, Sync/Gradient), each claim
> passed through 3-vote adversarial verification. Confidence and **refutations** are
> recorded inline — the refuted items are vendor numbers that should *not* be repeated
> as fact. Sources are cited by origin (primary docs vs vendor marketing).

## Purpose

Scopes a second product capability distinct from the existing TCO view: an
**efficiency / waste** view that surfaces the gap between what is billed and what is
actually used. This document is the evidence base; the design is in
[`../design/efficiency-waste.md`](../design/efficiency-waste.md).

---

## 1. "Shadow waste" — concept vs terminology

**The concept is FinOps-Foundation-grounded; the *name* is not.**

- ✅ **(high)** The FinOps Foundation defines **usage optimization** as *"ensuring a
  close match between the cloud resources provisioned and the needs of the business"*
  — i.e. **waste = the gap between provisioned and used**. Concrete threshold:
  resources at **0–20 % CPU/memory utilization are downsizing candidates**; rightsizing
  needs utilization telemetry over **≥ 60 days**. *(finops.org/wg/how-to-optimize-cloud-usage)*
- ✅ **(high)** FinOps Foundation "FinOps for Data Cloud Platforms": billing/warehouse
  views *"aggregate many workloads into a single consumption signal"* and *"rarely
  explain the execution behavior"* — they distribute cost but **cannot tell you whether
  the work was efficient**. This is the canonical justification for a separate view.
- ❌ **REFUTED (high, 3 votes)** "Shadow waste" is **NOT** a FinOps Foundation term. It
  is a **vendor-coined brand term** (Adaptive6 / Aviv Revach), presented as a *session*
  at FinOps X 2025 — not an official definition. **Do not attribute "shadow waste" to
  the FinOps Foundation in product copy.** Safer product language: **"efficiency waste"**
  or **"recoverable spend."**

**Shadow/efficiency waste vs TCO:**

| | TCO (existing) | Efficiency waste (new) |
|---|---|---|
| Question | What does it cost to run the platform? | How much of that cost did nothing? |
| Unit | Dollars billed (DBU + infra) | Dollars billed − dollars justified by use |
| On invoice? | Yes (it *is* the invoice) | **No** — invoice says "100 DBUs", never "90 idle" |
| Data | FOCUS / billing | **FOCUS insufficient — needs utilization telemetry** |

---

## 2. Waste taxonomy, split by data dependency

The split that drives the architecture: **billing/SKU-derivable** vs
**utilization-required**.

### Tier A — derivable from billing / SKU data (FOCUS-class)

About *rate* and *placement*, not utilization:

| Vector | Detection signal | Recoverable $ |
|---|---|---|
| **All-purpose compute running scheduled jobs** | `billing_origin_product` + `sku_name` (`STANDARD_ALL_PURPOSE_COMPUTE`) on scheduled work | ✅ **high** — all-purpose ≈ $0.40–0.55/DBU vs jobs ≈ $0.15/DBU → **~2.7–3.7× premium**. Highest-ROI single fix. |
| **Photon misapplied** | Photon flag on Python-UDF-heavy work | ✅ **high (Databricks' own blog)** — Photon multiplies DBUs **~2.9× (jobs) / ~2× (all-purpose)**; pure premium when it doesn't speed work up |
| **Idle clusters** (running, no jobs) | Cluster `RUNNING` vs job/query activity timestamps — **no CPU metric needed** | ✅ verified detectable; one case = **~15 % of monthly bill** (company-specific, *not* a benchmark) |
| **Failed / retried job spend** | `job_run_timeline.result_state IN ('ERROR','FAILED','TIMED_OUT')` ⋈ billing | ✅ **high** — Databricks ships the query |
| **On-demand where spot fits** | Cluster config spot vs on-demand | ⚠️ directional, single-source — spot discounts VM 60–90 %; ~30–45 % of total for fault-tolerant work |
| **Discount / commit under-realization** | list_cost vs effective_cost | ✅ already in GOLD `savings_summary_month` |

### Tier B — requires utilization telemetry (new data plane)

The "100 DBUs / 10 % CPU" core. Physically impossible from FOCUS:

| Vector | Signal source |
|---|---|
| **CPU/memory underutilization** | `system.compute.node_timeline` — `cpu_user_percent`, `cpu_system_percent`, `cpu_wait_percent`, `cpu_idle_percent`, `mem_used_percent`; **minute granularity**; `driver` boolean ✅ high |
| **Oversized node types** | `system.compute.clusters.{driver,worker}_node_type` + node_timeline |
| **Over-provisioned autoscaling** | `system.compute.clusters.{min,max}_autoscale_workers` — ranges that never move |
| **Missing / too-long auto-termination** | `system.compute.clusters.auto_termination_minutes` |
| **Query inefficiency / disk spill** | `system.query.history` — `total_duration_ms`, `produced_rows`, `spilled_local_bytes` (>1 GB = memory pressure) |

**Boundary (the architectural decision):** billing/FOCUS answers *who owns the spend*
and *what rate*; it cannot answer *was the work efficient*. Underutilization, spill,
and rightsizing **all require utilization telemetry**. ✅ high, corroborated by
Databricks docs + FinOps Foundation.

---

## 3. Data sources (Unity Catalog system tables)

Same warehouse the existing Databricks connector already queries.

| Table | Provides | Verified |
|---|---|---|
| `system.billing.usage` | DBUs, SKU, tags, `usage_metadata` (cluster_id/job_id/warehouse_id) — the join spine. **18 cols, no utilization, no $** (join `list_prices` for $) | ✅ high |
| `system.compute.node_timeline` | per-minute CPU/mem utilization, driver flag | ✅ high |
| `system.compute.clusters` | auto-termination, autoscale min/max, node types | ✅ high |
| `system.lakeflow.job_run_timeline` | job success/failure, run windows | ✅ high |
| `system.query.history` | per-query duration, rows, spill | ✅ high |
| `system.compute.warehouse_events` | SQL-warehouse idle/running-state % | ✅ |

**Gotchas (verified):**
- Databricks' ready-made **jobs-cost queries only cover jobs + serverless compute** —
  jobs on all-purpose & SQL warehouses are **excluded** (not billed as jobs). So the
  all-purpose-for-jobs vector must be detected via `sku_name` / `billing_origin_product`,
  **not** the jobs-cost queries.
- System-table queries are **region-scoped** — no data for workspaces outside the
  current region (limitation for multi-region rollups).
- Databricks usage dashboards = **visualization only** — no alerts, budgets, or
  automated waste detection. ✅ high.

---

## 4. Visualization patterns

**Rule from every source: the unit is recoverable dollars, not a utilization ratio.**
A 12 %-CPU gauge is a vanity metric; *"$3,200/mo recoverable by downsizing"* is action.
Databricks itself ships **"Top 25 Jobs by Potential Savings per Month."** ✅ high.

1. **Waste leaderboard ranked by $ recoverable** — top-N jobs/clusters/queries.
   *"Greatest savings is concentrated in heaviest spend"* → prioritize, don't enumerate.
2. **Utilization → cost bridge** — DBU billed × (1 − utilization), with the 0–20 %
   FinOps band as the flag.
3. **Idle / off-hours cost trend** — hourly cost exposing overnight/weekend burn,
   attributed to an owner.
4. **Rate-mix waste tiles** — all-purpose-vs-jobs & Photon premium (Tier A, no telemetry).
5. **Owner attribution** — `identity_metadata.run_as` / tags, so each line is actionable.

**Competitive framing:**
- **CloudZero / Unravel**: near-real-time + per-owner alerting (vs Databricks' ~24 h
  refresh, no alerts).
- **Gradient (Sync)**: closest architecturally — splits **DBU vs underlying infra**
  (our TCO decomposition) **plus per-job grain**, the granularity needed to localize waste.

---

## 5. Refuted / downgraded claims — do not repeat as fact

| Claim | Verdict |
|---|---|
| "Shadow waste" is a FinOps Foundation definition | ❌ **refuted (high)** — vendor term (Adaptive6) |
| "Auto-termination *eliminates* idle waste" | ❌ **refuted** — it *caps* it; you still pay DBU + VM for the full idle window before shutdown (UI default 120 min). Show **idle-window cost**. |
| "40 % of Databricks spend is waste, half from bad code" | ❌ **refuted on source quality** — single Unravel marketing blog, no methodology. Don't quote a headline waste %. |
| "Serverless costs 234 % more for analytical workloads" | ⚠️ **downgraded** — real *directional* point (placement matters), number is unverified vendor benchmarking |
| "Billing data alone cannot attribute *or detect* inefficiency" | ⚠️ **overreach** — billing **can** attribute *cost*; it cannot detect *behavioral inefficiency*. Keep the distinction precise. |

---

## Source provenance

- **Primary (high trust):** docs.databricks.com (system-tables: billing, compute,
  jobs-cost; cost-optimization best-practices), finops.org (usage-optimization WG,
  FinOps-for-Data-Cloud-Platforms, FinOps X agenda).
- **Vendor (directional, verify before quoting):** Unravel, CloudZero, Sync/Gradient,
  assorted Medium case studies. Treat all vendor magnitudes as directional.
