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

## Permissions and network access

The default Data API path requires AWS permissions appropriate for the configured cluster
and read-only system-table queries. Direct or bastion access additionally
requires a reachable endpoint and a read-only database identity. The optional bastion
path requires `getflashlight[redshift-bastion]`.

Start with a small date window and verify that AWS FOCUS data exists for the same time
range before assessing dollar-backed efficiency findings.
