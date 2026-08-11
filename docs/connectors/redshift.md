# Amazon Redshift

The Redshift connector collects efficiency and operational telemetry. It intentionally
does not ingest a second copy of Redshift cost. Enable an AWS FOCUS connector for the
same billing scope so Flashlight can associate AWS spend with the telemetry.

## Configure

```yaml
connectors:
  - type: redshift
    enabled: true
    name: Production cluster
    region: us-east-1
    cluster_identifier: my-redshift-cluster
    database: dev
    access_key_env: AWS_ACCESS_KEY_ID
    secret_key_env: AWS_SECRET_ACCESS_KEY
```

Flashlight supports provisioned Redshift clusters only. The generated example explains
Data API, direct database, and bastion-host authentication options.

## What it contributes

Flashlight collects query-pattern, user activity, workload management, and table-usage
signals. The resulting efficiency findings are guidance based on observed telemetry;
they never execute changes on the cluster.

Spectrum scan charges remain part of the Redshift AWS invoice. When a full telemetry
window and target-scoped FOCUS rows are available, Flashlight allocates that existing
charge across external tables by scanned bytes to make optimization actionable. This is
an estimate for attribution, not a new S3-storage charge or a second Redshift total.

## Permissions, calls, and query data

Flashlight does not submit DDL or DML. It submits read-only SQL against Redshift system
tables, then optionally reads two CloudWatch metrics. Use the Data API unless a direct or
bastion database connection is required by your network design.

### Default: Redshift Data API

Scope the IAM role to the configured provisioned cluster and grant:

| Permission | Why Flashlight needs it |
| --- | --- |
| `redshift-data:ExecuteStatement` | Submits each read-only telemetry statement. |
| `redshift-data:DescribeStatement` | Polls its status until it finishes. |
| `redshift-data:GetStatementResult` | Retrieves the result pages. |
| `redshift:GetClusterCredentialsWithIAM` or `redshift:GetClusterCredentials` | Needed when the selected Data API authentication path uses temporary database credentials. |
| `redshift:DescribeClusters` | Used only when Flashlight must discover the cluster endpoint for direct/bastion mode; set `db_host` and `db_port` to avoid this call. |
| `cloudwatch:GetMetricStatistics` | Optional: reads `CPUUtilization` and `PercentageDiskSpaceUsed` for one rolling 14-day aggregate. A denial does not stop SQL telemetry. |

If `secret_arn` is used for Data API database authentication, also grant read access to that
specific secret. AWS documents the Data API authorization model and its statement-owner
conditions in the [Redshift Data API IAM guide](https://docs.aws.amazon.com/redshift/latest/mgmt/data-api-iam.html).

### Direct or bastion SQL

Provide a read-only database identity that can run the system-table statements below.
System-table visibility is intentionally constrained by Redshift and varies by role; grant
only the visibility your operational policy permits. `STL_CONNECTION_LOG`, used for driver
health, is superuser-only. Flashlight treats that plane as optional, so a denial there does
not block the other telemetry. Direct access requires a reachable cluster endpoint; the
bastion path additionally requires `getflashlight[redshift-bastion]` and a reachable SSH
host.

### What the SQL reads

Each query is date-bounded where the source supports it. System-log retention is finite, so
Flashlight probes the earliest `stl_query` timestamp and records when a requested window is
only partly measurable instead of presenting old missing data as zero.

| Query group | System tables/views | Why it runs |
| --- | --- | --- |
| Cluster activity | `stl_query`, `svl_query_summary`, `stl_wlm_query`, `svcs_concurrency_scaling_usage` | Calculates query count, queue waits, spill count, and concurrency-scaling activity for the cluster-level efficiency assessment. |
| Query patterns | `stl_query`, `stl_wlm_query`, `svl_query_report`, `pg_user` | Finds recurring expensive/spilling query shapes and returns a bounded drill-down with a sample query and owner. |
| User activity | `stl_query`, `svl_query_report`, `svl_query_metrics_summary`, `pg_user` | Aggregates CPU, scan, spill, and duration share per user for workload concentration analysis. |
| Table and Spectrum observability | `svv_table_info`, `pg_tables`, `stl_scan`, `svl_s3query_summary`, `sys_external_query_detail`, `svv_external_tables` | Captures table size/ownership and recent internal/Spectrum scan facts before their source history rolls off. The external catalog is a current snapshot. |
| Driver health | `stl_connection_log` | Aggregates client driver/application and connection counts by month; it is a fleet-health signal, not a cost calculation. |

The complete, executable statements are part of the installed package and linked here for
review: [activity](https://github.com/getflashlight/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/redshift_efficiency.sql),
[query patterns](https://github.com/getflashlight/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/redshift_query_pattern_metrics.sql),
[user activity](https://github.com/getflashlight/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/redshift_user_activity.sql),
and [driver health](https://github.com/getflashlight/getflashlight/blob/main/src/flashlight/ingest/connectors/sql/redshift_driver_health.sql).

Sync at least six months and verify that AWS FOCUS data exists for the same time range
before assessing dollar-backed efficiency findings. Six months gives the efficiency views a
meaningful monthly baseline; use **Test connection** first if you need to diagnose access
without performing the initial historical pull.
