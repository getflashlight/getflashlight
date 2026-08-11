# Databricks

The Databricks connector runs a vendored Databricks System Tables-to-FOCUS 1.3 mapping
on a SQL warehouse, then writes the resulting canonical records. It can also collect
efficiency, driver-health, AI-usage, storage-location, and compute-inventory telemetry.

## Configure

```yaml
connectors:
  - type: databricks
    enabled: true
    name: Production workspace
    host: https://dbc-xxxxxxxx.cloud.databricks.com
    token_env: DATABRICKS_TOKEN
    sql_warehouse_id: null  # auto-select; set explicitly for production control
```

`DATABRICKS_TOKEN` must be available in the environment or local keychain path used by
Flashlight. Prefer a dedicated, read-only service principal where your deployment model
allows it.

## Permissions and queries

Flashlight uses the Statement Execution API to submit read-only SQL to one SQL warehouse.
The identity needs **Can Use** on that warehouse, plus Unity Catalog `USE CATALOG`, `USE
SCHEMA`, and `SELECT` access to the system schemas/tables below. If no warehouse is
configured, Flashlight lists available warehouses and picks the smallest usable one; set
`sql_warehouse_id` in production to make the compute and permission boundary explicit.

Start with the cost row below. The other rows are independent optional telemetry; a failure
is recorded and does not discard a successful cost pull. Some tables are account, region,
edition, or preview dependent. In particular, Databricks says `system.query.history` is
admin-only by default, so grant it deliberately rather than assuming cost access implies
query-history access.

| Plane | Tables or API queried | Why Flashlight calls it | Access to grant |
| --- | --- | --- | --- |
| FOCUS cost | `system.billing.usage`, `system.billing.list_prices` (and `system.billing.account_prices` when available), `system.access.workspaces_latest`, `system.lakeflow.pipelines`, `system.compute.clusters`, `system.compute.warehouses` | The vendored FOCUS 1.3 statement joins metered usage to the rate valid at that time, then adds readable workspace, pipeline, cluster, and warehouse context. A date predicate is appended to the final usage result for the ingest window. | SQL-warehouse use and read access to these billing, access, Lakeflow, and compute system tables. |
| Efficiency / waste | The cost tables above plus `system.compute.node_timeline`, `system.lakeflow.job_run_timeline`, `system.lakeflow.jobs`, `system.compute.node_types`, and `system.query.history` | Aggregates utilization proxies, job activity, warehouse wait/spill behavior, resource metadata, and pricing into candidate findings. It returns aggregates, not a raw query log. | The cost grants plus read access to the additional compute, Lakeflow, and query-history tables. |
| Driver health | `system.query.history` | Groups query counts by month, client driver, application, and executing identity for fleet visibility. It does not calculate spend or change driver configuration. | Query-history access; this is sensitive operational telemetry. |
| Compute inventory | `system.compute.node_timeline`, `system.compute.clusters`, `system.lakeflow.jobs` | Maps classic Databricks instances to clusters and jobs so AWS EC2 spend can be labelled. Serverless compute has no customer-visible instances in this table. | Read access to these compute and Lakeflow tables. |
| AI usage | `system.serving.endpoint_usage`, `system.serving.served_entities`, and billing/pricing tables | Aggregates requests, token counts, errors, model/endpoint metadata, and the related billed usage by endpoint-month. The serving tables are probed first; Flashlight skips or degrades this plane when they are absent. | Read access to enabled `system.serving` tables plus the relevant billing tables. |
| Storage-location map | Metastore summary, catalog list, and external-location list through the Workspace API; `DESCRIBE DETAIL` on accessible candidate tables | Maps Unity Catalog storage URLs to Databricks metadata so AWS storage can be labelled. It does not read object contents or calculate a second storage cost. | Metastore/catalog/external-location visibility. These APIs are privilege-filtered: a non-admin identity can return a partial map without an error. |

The exact statements are versioned with the package rather than copied into this guide:

- [FOCUS cost mapping](https://github.com/ychaparala/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/databricks_focus_1_3.sql)
- [Efficiency aggregation](https://github.com/ychaparala/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/databricks_efficiency.sql)
- [Driver-health aggregation](https://github.com/ychaparala/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/databricks_driver_health.sql)
- [Compute-instance aggregation](https://github.com/ychaparala/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/databricks_compute_instances.sql)
- [AI-usage aggregation](https://github.com/ychaparala/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/databricks_ai_usage.sql)

These statements only submit `SELECT`, `DESCRIBE DETAIL`, and metadata-list operations;
Flashlight does not issue DDL, DML, or permission changes against Databricks. See the
[Databricks system-tables reference](https://docs.databricks.com/aws/en/admin/system-tables/)
and the [query-history access guidance](https://docs.databricks.com/aws/en/admin/system-tables/query-history)
when granting access.

## Mapping and semantics

The connector uses the documented vendored SQL rather than reimplementing billing math
in Python. Flashlight adds `x_compute_class` where needed to distinguish classic and
serverless context not represented by a universal FOCUS field.

For AI costs, token measurements are stored separately from billing. A dollar-per-token
claim is made only where the serving model supports that relationship. Read
[AI costs and usage](../design/ai-costs.md) before interpreting the figures.
