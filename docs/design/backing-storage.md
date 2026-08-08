# Backing storage — the cloud storage bill behind Databricks

## The problem

Databricks' FOCUS bill (`system.billing.usage`) covers **DBU compute only**. Storage lives
in customer-owned S3 buckets and is billed by AWS. So "what does Databricks storage cost?"
had no answer anywhere in Flashlight: the Databricks plane doesn't contain it, and the AWS
plane contains it but can't tell you which buckets are Databricks'.

Unity Catalog knows which buckets Databricks **manages**. The AWS FOCUS export knows what
those buckets cost. This feature joins the two — and is careful about which buckets qualify,
because "Unity Catalog can see it" and "Databricks pays for it" are not the same set.

## What counts: managed storage only

Not everything Unity Catalog can see is Databricks' storage cost. Two relationships look
alike in the metadata and are completely different in a bill:

| | **Managed** storage | **External** location |
|---|---|---|
| Who provisioned it | Databricks | it already existed |
| Drop the UC object | the data is **deleted** | the data is **untouched** |
| Whose cost is it? | Databricks' | whoever owns that pipeline's |
| Counted here? | **yes** | **no** |

So `mapping = 'databricks'` means **the bucket holds the Unity Catalog metastore root**.
External locations are excluded on purpose: that data exists whether or not Databricks reads
it, so costing it here would **double-claim** another team's data-lake spend against
Databricks. Catalog roots and external locations are still *recorded* in
`storage.storage_location` — the inventory is the audit trail that answers "why isn't this
bucket counted?" — but only metastore roots cost anything.

**This makes the figure a floor, not a total.** Per-workspace DBFS root buckets and any
catalog whose `storage_root` sits on its own bucket are *also* Databricks-provisioned managed
storage, and both read as `unmapped` (see limitation #2). Under-claiming beats double-claiming,
but the dashboard states it rather than leaving it to be discovered.

## Two bills, never one number

**The rule the whole feature is built around:** this money is billed by AWS, is already
counted in `aws.monthly_bill`, and is **never added to Databricks spend**. Summing DBU cost
and the AWS infrastructure behind it is exactly the TCO capability CLAUDE.md removed, and
nothing here reintroduces it.

What makes that enforceable rather than merely intended:

* The views live in their own fixed GOLD group (`storage`), so nothing writes into
  `gold/databricks/`. `databricks.monthly_bill` and the Databricks KPIs are untouched **by
  construction** — asserted directly in
  `tests/test_lake_roundtrip.py::test_backing_storage_never_changes_databricks_spend`.
* Every row carries **two** provider columns — `billing_provider_name` (who invoices it:
  AWS) and `platform_provider_name` (whose metadata claims the bucket: Databricks) — so a
  consumer cannot mistake one for the other.
* The join is AWS **cost** to Databricks **metadata** (a bucket list). It is never AWS cost
  to Databricks cost.
* The GOLD `description` strings carry the rule, not just the dashboard caption, because MCP
  and the assistant read those descriptions. `assistant_engine._PLAN_INSTRUCTIONS` states it
  too.

## How it works

### 1. Discovery — Unity Catalog's bucket map

`DatabricksConnector.fetch_storage_locations` (a best-effort `Connector` hook alongside
`fetch_efficiency` / `fetch_driver_health` / `fetch_ai_usage`) reads three Unity Catalog
surfaces via the Databricks SDK:

| Source | `location_kind` | Field |
|---|---|---|
| `metastores.summary()` | `metastore_root` | `storage_root` |
| `catalogs.list()` | `catalog` | `storage_root` (falling back to `storage_location`) |
| `external_locations.list()` | `external_location` | `url`, `read_only`, `credential_name` |

All three are **pure REST — no running SQL warehouse required**, unlike every other pull in
that connector, so the map still refreshes on a workspace whose warehouse is stopped.

Each listing is guarded independently: a token that can read external locations but not the
metastore summary still yields the external locations. Losing one source degrades coverage;
it does not blank the map.

`_parse_storage_url` resolves each URL to `(scheme, cloud_provider_name, bucket_name,
key_prefix)`. **`key_prefix is None` means the URL addresses the bucket root** — that
distinction carries the whole mapping-confidence signal below and is never collapsed to an
empty string.

### 2. Persistence — a third medallion plane

`StorageLocationRecord` → `storage_locations/` Parquet (`provider_name` /
`snapshot_month`) → `metrics.storage_location`. Deliberately **not** an `EfficiencyRecord`
with a new `EntityType`: `gold.efficiency_entity_month` is the coverage *denominator* that
`efficiency_waste.coverage_caption()` exists to keep honest, and metadata rows would inflate
the "measured" count — corrupting exactly the number that prevents over-claiming.

Two deviations from its sibling planes, both intentional:

* **`snapshot_month`, not `charge_month`.** UC exposes only current state, so this is a
  point-in-time inventory stamped with the month it ran in (the same call
  `_fetch_table_inventory` makes). It is correspondingly *not* in `PERIOD_DIMENSIONS` — you
  cannot trend along it.
* **`write_storage_locations` takes no `IngestWindow`**, and an empty pull is a **no-op, not
  a purge**. Unlike a cost window — where "the source no longer reports this month" is real
  information and self-purging is the point — an empty metadata pull means the API call
  failed or a grant is missing. Deleting a good map would turn a transient permission
  problem into permanent data loss, and would make the tab imply Databricks has no storage
  cost.

Older snapshots are kept as the audit trail for "when did this bucket become
Databricks-backed?"; `backing_storage_month` reads only the newest.

### 3. The cost side — S3 in the AWS pull

`AwsFocusConfig.include_services` now defaults to Redshift's services **+ Amazon S3**
(`DEFAULT_INCLUDE_SERVICES`). The alternative — pushing a UC-derived bucket allow-list into
the scan predicate — was rejected: it would make the AWS pull depend on the Databricks pull
having run first, and it would leave the mapped figure with no denominator.

`aws_focus._cost_subcategory_sql()` stamps `x_cost_subcategory` on S3 rows —
`storage` / `requests` / `data_transfer` / `monitoring` / `early_delete` / `other`. The split
matters because Databricks drives heavy LIST/GET metadata traffic: a request-volume problem
and a storage-growth problem look identical in one total and have completely different
remedies.

Both service families compose into **one** CASE expression, because
`sql_mapping.mapping_sql` accepts exactly one `cost_subcategory_sql`. `ChargeCategory =
'Credit'` is hoisted above both branches; `NULLIF(…, 'committed')` stays on the Redshift
branch only (S3 has no commitment SKUs).

### 4. GOLD — `storage.backing_storage_month`

`065_gold_storage.sql` keeps **every** S3 row, labelled:

| `mapping` | meaning |
|---|---|
| `databricks` | the bucket holds the UC **metastore root** — managed storage |
| `unmapped` | it doesn't. Includes external-location buckets, **deliberately** |
| `no_resource_id` | S3 cost carrying no ResourceId at all — attributable to no bucket |

| `mapping_confidence` | meaning |
|---|---|
| `whole_bucket` | the metastore root addresses the bucket root, so the whole bucket is Databricks storage |
| `prefix_scoped` | it claims a key prefix only — the cost is an **upper bound**. The usual case, since a metastore root is normally `s3://bucket/<metastore-id>` |
| `n/a` | not managed storage |

Three implementation details are load-bearing rather than cosmetic:

* **`location_kind = 'metastore_root'`** is where the managed-only rule is enforced. It also
  gives managed storage **precedence** for free: a bucket that is both a metastore root and an
  external-location target (common — a catalog's `storage_root` often sits inside an external
  location's path) still matches, so it reads `databricks` rather than being demoted.
* **One row per bucket** in the `bucket_map` CTE. A metastore root can be reported more than
  once (several metastores, or a re-pull mid-window); joining raw location rows would multiply
  that bucket's cost by the number of locations — inventing spend that is not on the bill.
* **`coalesce(x_cost_subcategory, '(unclassified)')`.** Unlike
  `gold.spend_by_cost_subcategory_month`, an unclassified row must not be dropped here: it's
  part of the denominator, and a lake ingested before the classifier shipped has NULL for
  every S3 row.

Summing `net_cost` across all `mapping` values reproduces the account's whole S3 bill — the
contract asserted in `test_backing_storage_accounts_for_every_s3_row` and in
the schema-driven demo generator's GOLD contract audit.

### 5. The dashboard

`Databricks → Backing storage` (`views/backing_storage.py`). Two producers
(`aws_focus` for the cost, `databricks` for the map) but one *subject*, which is why it's an
`extra_tabs` entry on the Databricks page rather than a nav entry of its own.

Two panels: one table of Databricks-managed buckets (metastore roots first, then catalogs) and
the monthly cost split by S3 charge type.

**A KPI card on the page itself**, not only inside the tab (`kpi_card`, passed to
`provider_focus.render` as `extra_kpis`): "what does Databricks cost me?" is asked at the top
of the page, and a number a tab away is a number most readers never see. It is windowed to the
page's date control, sits beside `Databricks net` and never inside it — the sub-line says
`AWS-billed S3 · not in net · a floor` and the card takes its own hue so it doesn't read as
another slice of one total. It is omitted entirely, never `$0`, when nothing is mapped: a zero
there would answer "what does Databricks storage cost?" with "nothing" on precisely the lake
that has not looked yet. Each empty state still names its own cause — a gap
that only appears once it's already large is a gap nobody plans around — and the empty-map
state still states the S3 denominator.

**The lead prose is gone.** It opened with four paragraphs above the first number: the
two-bills caption, a coverage caption (managed share of the S3 bill with the prefix-scoped
caveat), the floor disclosure and the gap list. Removed on request as wall-of-text. The
*rules* are unchanged and still recorded here, in CLAUDE.md and in `065_gold_storage.sql`'s
header — the figure is still a floor, workspace DBFS roots and per-catalog storage roots are
still uncounted, and the two bills must still never be summed. What carries that in the UI is
per-row rather than prose: the cost figure itself (`≤ $…` for a prefix-scoped upper bound,
`$… (shared)` when several catalogs share a bucket, bare `$…` when ownership is whole-bucket)
plus a one-line caption under the table explaining `≤`, and the `S3 cost (AWS-billed)`
column heading. The managed **share** of total S3 spend is no longer
on screen at all; it's a GOLD/MCP query.

**One table, not two.** The managed-bucket list and the per-catalog list were separate panels
showing the same rows twice — each managed object sits on its own bucket, so bucket-grained
cost *is* object-grained cost. They're merged: bucket, owning catalog/metastore, kind.

**No per-bucket list of unmanaged buckets.** It was there originally as the visible
denominator, but on a real account (2,008 S3 buckets, one metastore root) a 20-row table of
unrelated buckets buried the single number the tab exists to report, and the tab read as "here
is all your S3 spend". Per-bucket detail stays queryable via `storage.backing_storage_month`
in GOLD/MCP.

## Known limitations

Investigated and recorded here rather than silently left as gaps.

1. **The AWS bill's S3 grain is the bucket; Unity Catalog's grain is the prefix.** A FOCUS
   S3 row's `ResourceId` is the bucket ARN — there is no per-prefix cost anywhere in the
   export. So when a UC external location points at `s3://bucket/prefix`, the bucket's cost
   **cannot be split** between Databricks and whatever else lives there. Surfaced as
   `mapping_confidence='prefix_scoped'` and rendered with an explicit `≤` on the cost
   figure, never as a bare number. There is no fix short of S3 Storage Lens prefix
   metrics or per-prefix inventory, both separate ingestion pipelines.

2. **Workspace DBFS root buckets are out of scope, so the figure is a floor.** A workspace's
   root bucket is not a Unity Catalog object, so none of
   `metastores`/`catalogs`/`external_locations` reports it — its cost lands in `unmapped`
   even though it *is* Databricks-provisioned managed storage. Same for a catalog whose
   `storage_root` sits on its own bucket (recorded as `location_kind='catalog'`, not costed).
   Stated in the tab's floor disclosure so it can't read as "we checked and there's nothing
   there".

   This is **fixable, and deliberately deferred rather than impossible.** Verified against the
   installed SDK: `AccountClient.storage.list()` returns `StorageConfiguration` with
   `root_bucket_info.bucket_name`, attributable per workspace via
   `AccountClient.workspaces.list().storage_configuration_id`. It needs **account-level**
   credentials — a Databricks `account_id` plus an account-admin service principal, distinct
   from the current workspace host + PAT — which is why it isn't wired up. If someone revisits
   this: add it as a fourth best-effort source in `_storage_location_sources()` with
   `location_kind='workspace_root'`, gated on the new optional config and skipped silently
   when absent, exactly like every other source there. An explicit `extra_storage_buckets`
   list would cover the residue (legacy mounts) as `location_kind='declared'` — kept a
   distinct kind so a human assertion never masquerades as a UC-discovered fact.

   Explicitly rejected: inferring Databricks ownership from bucket **naming patterns**
   (`dbrk-*`, `d??-prd-<hash>`). On a real account these sit beside application buckets that
   no rule can distinguish, and a guess rendered as a mapping is precisely what the confidence
   labelling exists to prevent.

3. **The map is a snapshot applied to history.** `backing_storage_month` uses the newest
   `snapshot_month` for every month of cost, so a bucket registered last week is credited
   with its earlier cost too. Usually right — the data generally predates the registration —
   but it is an assumption, which is why the caption says the map is "as of the last sync".
   Earlier snapshots remain in `metrics.storage_location` for auditing.

4. **A `cost_source="cost_explorer"` AWS connection can never map a bucket.**
   `_map_ce_group` never sets `resource_id` (Cost Explorer returns account-level SERVICE
   totals), so all of its S3 cost lands in `mapping='no_resource_id'`. This is a first-class
   value rather than a footnote, and the gap caption reports its dollar total.

5. **Non-AWS UC locations don't join.** `abfss://`/`gs://` locations are recorded (and
   counted in the gap caption) but there is no Azure or GCP cost connector to label, so they
   can never become mapped cost rows.

6. **`schemas.list()` is deliberately not pulled.** One REST call per catalog (an N+1
   against a large metastore), and a schema's managed location is virtually always under its
   catalog's root — so it would add bucket coverage almost never. Revisit if a real account
   turns out to put schemas on their own buckets. `tables.list()` is out of scope for the
   same reason at a larger scale.

7. **⚠ The S3 subcategory keyword table is UNVALIDATED against a live FOCUS export.**
   `_S3_CATEGORY_RULES` in `aws_focus.py` is a text-match heuristic over
   `ChargeDescription` + `SkuId`, exactly like its Redshift sibling — and that sibling needed
   **two** corrections after meeting real billing text (`instance` and `data scan`, without
   which the single largest line item in the account, $44K, sat silently in `other`). Assume
   this one needs the same.

    **To validate:** run `flashlight ingest` against a real AWS account with S3 in
    `include_services`, then check the distribution:

    ```sql
    SELECT cost_subcategory,
           count(*)              AS rows_seen,
           round(sum(net_cost),2) AS total_cost
    FROM storage.backing_storage_month
    GROUP BY cost_subcategory
    ORDER BY total_cost DESC;
    ```

    A large `other` or `(unclassified)` bucket means keywords are missing. Cross-check the
    raw text with `SELECT DISTINCT charge_description, sku_id FROM raw.focus_record WHERE
    service_name = 'Amazon Simple Storage Service'` and correct the table.

    The open question underneath it: AWS's discriminating **usage type**
    (`TimedStorage-ByteHrs`, `Requests-Tier1`, `DataTransfer-Out-Bytes`) is what these
    keywords really target, and whether it reaches us in `SkuId` at all in the FOCUS 1.2
    export is unconfirmed. If it doesn't, the fix is an `x_UsageType` column — which means
    adding to `_FOCUS_COLUMNS` **and recreating the export** (the FOCUS table version is
    baked into it), so the design deliberately doesn't assume it.

    This is the same open question as the `s3_intelligent_tiering` rule's caveat in
    `efficiency/waste_rules.py`, over the same text: **validating one validates the other.**

8. **The `s3_intelligent_tiering` waste rule became reachable with this change.** It was
   written but dead, because S3 rows never entered BRONZE under the old Redshift-only
   default. Its findings (`provider_name='AWS'`, `entity_type='storage'`) now appear in
   `efficiency.waste_record` and `efficiency.waste_by_owner_month`. Findings on *unmapped*
   buckets exist in GOLD and MCP with no dashboard panel of their own — a known gap, left
   visible rather than suppressed: a working signal is not a bug.

## Migration notes

* **The AWS group's totals grow by the S3 bill** on the next ingest — Home's AWS card,
  `aws.monthly_bill`, `spend_by_service_month`, `invoice_reconciliation_month` and tag
  coverage all move. `/aws` does **not** (it is Redshift-scoped), so its scope caption now
  says so explicitly and points S3 at the Backing storage tab.
* **The `/aws` display label is now derived.** `data._aws_label` reads "AWS Redshift" while
  every service in the group is one of Redshift's own, and plain "AWS" once it holds more.
  It fails toward the *narrower* label on any query problem — claiming less than the group
  holds is a smaller lie than implying the whole account is there.
* **Only the re-pulled window changes.** `bronze.write_window` is authoritative per
  (connector, window) and the CLI default is a 35-day lookback, so an existing lake gets a
  step change at the window boundary: older months Redshift-only, recent months with S3. Run
  `flashlight ingest --start <YYYY-MM-01>` once to backfill.
* **Bytes scanned goes up** — S3 line items at resource grain are numerous. The service
  predicate is still pushed down into the Parquet scan, so this is a linear cost, not a
  behaviour change.
* **Opting out** stays a one-line edit: set `include_services` to just Redshift's service
  names in `connections.yml`.
