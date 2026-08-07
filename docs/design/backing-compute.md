# Backing compute — the cloud compute bill behind Databricks

## The problem

Databricks' FOCUS bill (`system.billing.usage`) covers **DBU compute only**. For a
CLASSIC (non-serverless) cluster, Databricks orchestrates the creation of the underlying
compute dynamically on the customer's own cloud account — on AWS, an EC2 instance — and
that instance is billed separately, by AWS. So "what does a Databricks cluster's cloud
infrastructure cost?" had no answer anywhere in Flashlight: the Databricks plane doesn't
contain it, and the AWS plane contains it but can't tell you which instances are
Databricks'.

Databricks' own `system.compute.node_timeline` system table knows which cloud instance
backed which cluster (`instance_id`, e.g. `i-1234a6c12a2681234` on AWS). The AWS FOCUS
export knows what that instance costs. This feature joins the two — the identical shape
as [backing storage](backing-storage.md), for compute instead of storage, and careful
about the same class of question: "Databricks system tables can see it" and "Databricks
pays for it" are not the same set once serverless compute is in the mix.

## What counts: classic compute only

Databricks compute comes in two shapes that look similar in the billing plane and are
completely different in whether a cloud VM exists at all:

| | **Classic** compute | **Serverless** compute |
|---|---|---|
| Who provisions the VM | Databricks, in the customer's own cloud account | Databricks, in its own multi-tenant control plane |
| Customer-visible instance? | Yes — `system.compute.node_timeline` reports it | **No** — nothing to report |
| Separate cloud infra bill? | Yes (EC2 on AWS) | No — one bill, DBU only |
| Counted here? | **yes** | **no, by construction** |

So this feature's map can only ever cover classic all-purpose clusters, classic jobs
clusters, Lakeflow-pipeline compute and pipeline-maintenance compute — the compute
classes `system.compute.node_timeline` reports on. Serverless SQL warehouses, serverless
jobs and DLT serverless pipelines have **zero rows** there; there is no customer-visible
instance for Databricks to hand back. `x_compute_class` (stamped at ingest, see
`connectors/databricks.py`) already carries this distinction descriptively, though no
GOLD view keys off it directly — this feature's absence-of-a-map is the practical
consequence of the same classic/serverless split.

**This makes the figure a floor, not a total**, and the gap is structural rather than an
edge case: as serverless adoption grows, a larger share of real DBU spend has no cloud
VM behind it at all to attribute.

## Two bills, never one number

**The rule the whole feature is built around:** this money is billed by AWS, is already
counted in `aws.monthly_bill`, and is **never added to Databricks spend**. Summing DBU
cost and the cloud infrastructure behind it is exactly the TCO capability CLAUDE.md
removed, and nothing here reintroduces it.

What makes that enforceable rather than merely intended (identical mechanism to backing
storage):

* The views live in their own fixed GOLD group (`compute`), so nothing writes into
  `gold/databricks/`. `databricks.monthly_bill` and the Databricks KPIs are untouched **by
  construction**.
* Every row carries **two** provider columns — `billing_provider_name` (who invoices it:
  AWS) and `platform_provider_name` (whose metadata claims the instance: Databricks) — so
  a consumer cannot mistake one for the other.
* The join is AWS **cost** to Databricks **metadata** (an instance/cluster map). It is
  never AWS cost to Databricks cost.
* The GOLD `description` strings carry the rule, not just the dashboard caption, because
  MCP and the assistant read those descriptions. `assistant_engine._PLAN_INSTRUCTIONS`
  states it too.

## How it works

### 1. Discovery — Databricks' own instance/cluster map

`DatabricksConnector.fetch_compute_instances` (a best-effort `Connector` hook alongside
`fetch_efficiency` / `fetch_driver_health` / `fetch_storage_locations` /
`fetch_ai_usage`) runs its own small vendored query
(`connectors/sql/databricks_compute_instances.sql`) against
`system.compute.node_timeline` on a SQL warehouse — unlike storage locations' pure-REST
pull, this needs a running warehouse, the same requirement every other row-based pull in
that connector has.

The query is one row per `(cluster_id, instance_id, charge_month)`, aggregated with
`ANY_VALUE(driver)`/`ANY_VALUE(node_type)` — lossless, not a "pick a representative
value" approximation, because an instance's driver/worker role and node type don't
change within its lifetime.

### 2. Persistence — a genuine charge-period fact, not a snapshot

`ComputeInstanceRecord` → `compute_instances/` Parquet (`provider_name` /
`charge_month`) → `metrics.compute_instance`.

This is the one structural place backing compute differs from backing storage, and it's
an improvement, not just a variation: Unity Catalog's bucket map is a **snapshot** (UC
exposes only current state), so storage needs a "no-op on empty pull" writer and applies
its newest snapshot retroactively across every month of cost. `system.compute.node_timeline`
instead reports **bounded historical activity** — its rows carry `start_time`/`end_time`
within the pull's own window — so a pull for a given window is authoritative for that
window, and `lake.compute_instances.write_compute_instances` does a real
partition-replace by `(provider_name, charge_month)`, the identical pattern
`DriverHealthRecord` uses. The GOLD join (below) matches an instance's cluster
membership against the *exact* month it actually ran in, not a "map as of last sync"
assumption.

The practical cost of this honesty: `system.compute.node_timeline` retains only ~90
days, so the map is only as good as what was actually captured while a window was
ingested. There is no way to backfill a cluster's EC2 history for a window Flashlight
never pulled — unlike storage's snapshot, which can (mostly correctly) be applied
backwards.

### 3. The cost side — EC2 in the AWS pull

`AwsFocusConfig.include_services` now defaults to Redshift's services + S3 + EC2
(`DEFAULT_INCLUDE_SERVICES`), the identical default-inclusion decision made for S3. The
alternative — leaving EC2 opt-in — was rejected for the same reason S3 wasn't left
opt-in: it would leave the mapped figure with no denominator by default, and this
feature exists to answer "capture all EC2 cost automatically, then label the Databricks
share of it" (see the feature's own motivating ask).

⚠ **The exact FOCUS `ServiceName` value for Amazon EC2 is UNVALIDATED against a live
export** (`ingest/_ec2_service_names.py`) — same caveat class as the S3/Redshift keyword
tables, which both needed corrections after meeting real billing text. This repo's own
test fixtures disagree with each other on the string (`"AmazonEC2"`,
`"Amazon Elastic Compute Cloud - Compute"`); confirm the real value against a live
export before relying on this in production.

### 4. GOLD — `compute.backing_compute_month`

`066_gold_compute.sql` keeps **every** EC2 row, labelled:

| `mapping` | meaning |
|---|---|
| `databricks` | this EC2 instance backed a Databricks cluster (matched by instance id AND charge_month against `system.compute.node_timeline`) |
| `unmapped` | it didn't. Includes non-instance EC2-service resources (EBS volumes, Elastic IPs, …), **deliberately** — they carry a ResourceId but can never match an instance map |
| `no_resource_id` | EC2 cost carrying no ResourceId at all — attributable to no instance |

`instance_role` is `'driver'` / `'worker'` / `'n/a'` (unmapped).

Three implementation details are load-bearing rather than cosmetic:

* **The join key is `(instance_id, charge_month)`, not just `instance_id`.** Because the
  Databricks-side map is a genuine per-month fact (see §2), matching per month is
  strictly more honest than storage's single-snapshot-for-all-history join: an instance
  whose activity Flashlight never captured (predates the ingest history, or predates
  `node_timeline`'s retention at the time of a given pull) correctly reads as `unmapped`
  for that month, without corrupting months it *was* captured for.
* **One row per `(instance_id, charge_month)`** in the `instance_map` CTE. This is a
  safety net, not an expected real case — AWS never reissues a terminated instance's id,
  so genuine duplicates shouldn't occur — but it's the same multiply-counting guard
  `bucket_map` uses in `065_gold_storage.sql`.
* **The ResourceId parse falls back to the raw value, unconditionally**, exactly like
  S3's bucket-ARN parse: `arn:aws:ec2:region:account:instance/i-xxx` extracts the instance
  id; anything else (a bare `i-xxx`, or a non-instance resource like `vol-xxx`) is used
  as-is. A non-instance resource simply never matches anything in `instance_map`, so it
  correctly reads `unmapped` rather than needing a separate "is this even an instance"
  check.

Summing `net_cost` across all `mapping` values reproduces the account's whole EC2 bill —
pinned by `test_backing_compute_accounts_for_every_ec2_row`.

### 5. The dashboard

`Databricks → Databricks Compute` (`views/backing_compute.py`). Two producers
(`aws_focus` for the cost, `databricks` for the instance/cluster map) but one *subject*,
which is why it's an `after_breakdown` entry on the Databricks page rather than a nav
entry of its own — the identical nesting rationale as Backing storage.

Two panels: one table of Databricks-managed clusters (cluster id, EC2 instance count,
mapped cost) and the monthly cost split by driver vs. worker role.

**A KPI card on the page itself** (`kpi_card`, wired through `provider_focus.render`'s
`extra_kpis`), sharing Backing storage's hue: both cards mean the same thing to a
reader — a satellite AWS bill, not a slice of `Databricks net`. It is **omitted, never
rendered as `$0`**, when nothing is mapped: a zero there would answer "what does this
cluster's cloud infra cost?" with "nothing" on precisely the lake that has not looked
yet, or a lake made entirely of serverless clusters (a real, growing case, not just a
transient gap).

**No per-instance list of unmapped EC2 resources** — same reasoning as backing storage's
missing per-bucket list: on a real account with a large unrelated EC2 fleet, a table of
everything unmanaged buries the one number the tab exists to report. Per-row detail
stays queryable via `compute.backing_compute_month` in GOLD/MCP.

## Known limitations

Investigated and recorded here rather than silently left as gaps.

1. **Classic compute only, so the figure is a floor — and a growing one.** Serverless
   SQL warehouses, serverless jobs and DLT serverless pipelines have no customer-visible
   instance at all; `system.compute.node_timeline` reports zero rows for them. There is
   no fix within this design — the whole point of Databricks serverless is that the
   customer never sees (and Databricks never has to disclose) the underlying VM. A
   cluster on this tab's "unmapped" or absent list may simply be serverless, not
   unmeasured.

2. **`system.compute.node_timeline`'s ~90-day retention bounds the map**, in both
   directions: an instance's activity that predates the retention window *at the time
   Flashlight ingested it* can never be recovered, and there is no account-level API that
   exposes historical instance/cluster membership the way `AccountClient.storage.list()`
   does for workspace storage roots (backing storage's own limitation #2). Re-running
   `flashlight ingest` promptly and on a normal cadence is the only mitigation.

3. **Non-instance EC2-service resources (EBS volumes, Elastic IPs, NAT gateways, …) read
   as `unmapped`, not as a separate category.** They genuinely are EC2-service AWS cost
   with a real ResourceId, they simply never match an `instance_id`, so they fall into
   the same bucket as EC2 workloads unrelated to Databricks entirely. Not split out
   further because the distinction (attached-to-a-Databricks-instance EBS volume vs.
   unrelated EBS volume) needs an EBS-volume→instance attachment map Databricks exposes
   nowhere — deferred, not attempted.

4. **A `cost_source="cost_explorer"` AWS connection can never map an instance.**
   `_map_ce_group` never sets `resource_id` (Cost Explorer returns account-level SERVICE
   totals), so all of its EC2 cost lands in `mapping='no_resource_id'` — the identical
   limitation backing storage has for S3.

5. **Deferred alternative: AWS cost-allocation tags.** Every EC2 instance Databricks
   provisions is stamped with `default_tags` including `ClusterId` and
   `Vendor=Databricks`. If a customer activates these as AWS cost-allocation tags, the
   AWS FOCUS export's `Tags` map would carry them, giving a join key immune to
   `node_timeline`'s retention and to instance churn under autoscaling/spot replacement
   (a `cluster_id` is stable for the cluster's life; instance ids under it are not).
   Deliberately out of scope for this pass — it needs a customer-side AWS Console action
   this design doesn't assume, and the `node_timeline`-based join above is self-contained
   (needs nothing from the customer's AWS account beyond EC2 already being in
   `include_services`). Revisit if `node_timeline`'s retention or classic-only scope
   proves too narrow in practice: it would be a second, independent source feeding the
   same `metrics.compute_instance` plane, not a replacement for it.

6. **⚠ The EC2 FOCUS `ServiceName` value is UNVALIDATED against a live export** (see §3
   above) — validate before relying on this in production, the same open question S3's
   own subcategory keyword table carries.

## Migration notes

* **EC2 is now captured by default** — the next `flashlight ingest` on an existing lake
  pulls EC2 rows into BRONZE for every AWS connection that hasn't overridden
  `include_services`, exactly like S3's own migration. Bytes scanned grows accordingly
  (EC2 line items at resource grain are typically the single largest, most numerous
  category in an AWS account) — the service predicate is still pushed down into the
  Parquet scan, so this is a linear cost, not a behaviour change.
* **The AWS group's totals are unaffected** — `silver.focus_provider_bill` excludes EC2
  from `aws.*` GOLD the same way it already excludes S3, so `aws.monthly_bill` and every
  view built on it are untouched by this feature.
* **Only the re-pulled window changes**, same as storage's own migration note: an
  existing lake gets a step change at the window boundary (older months without EC2/the
  compute map, recent months with both). Run `flashlight ingest --start <YYYY-MM-01>`
  once to backfill.
* **Opting out** stays a one-line edit: set `include_services` to just Redshift's (and
  optionally S3's) service names in `connections.yml`.
