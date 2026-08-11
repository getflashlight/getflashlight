# AWS

Flashlight supports AWS cost ingestion from an AWS Data Exports FOCUS dataset (preferred)
or from Cost Explorer (fallback).

## Preferred: AWS FOCUS Data Export

Create an AWS Data Export in FOCUS format, deliver it to S3, and point an `aws_focus`
connection at its bucket and prefix. Flashlight scans the published Parquet data and
applies only light coercion because the export is already FOCUS-shaped.

```yaml
connectors:
  - type: aws_focus
    enabled: true
    name: Production AWS
    s3_bucket: my-finops-exports
    s3_prefix: focus/flashlight/data
    region: us-east-1
    access_key_env: AWS_ACCESS_KEY_ID
    secret_key_env: AWS_SECRET_ACCESS_KEY
```

Use the AWS helper commands to provision and inspect an export:

```bash
flashlight aws create-export --bucket my-finops-exports --prefix focus/flashlight/data
flashlight aws describe-export
flashlight aws bucket-policy --bucket my-finops-exports
```

The export delivery destination must permit AWS Data Exports to write its data. Review
the generated bucket-policy guidance before applying any policy.

## Cost Explorer fallback

Set `cost_source: cost_explorer` when a FOCUS export is not available. It avoids S3/export
setup, but returns coarser account-level totals and less charge detail than the FOCUS path.
Use it for an initial evaluation, not when per-resource or detailed allocation analysis is
required.

## Permissions and calls

Choose the permission set for the path you actually configure. Flashlight never modifies
cost data, S3 objects, or billing settings during `flashlight ingest`.

### FOCUS export in S3

Grant the ingest identity these read-only permissions, scoped to the configured export
bucket and prefix:

| Permission | Scope | Why Flashlight calls it |
| --- | --- | --- |
| `s3:ListBucket` | The export bucket, with an `s3:prefix` condition for the export root | Lists `metadata/` to find the current manifest for each billing month. |
| `s3:GetObject` | The manifest objects and Parquet objects below that prefix | Reads each manifest, then scans only the Parquet files named by that manifest. |

The manifest step is deliberate. AWS can retain multiple deliveries for a month; reading
the manifest avoids treating historical delivery copies as additional charges. DuckDB then
scans the selected Parquet files with the requested date window and optional
`include_services` filter pushed into the scan. It reads source billing data only; the
mapped records are written to Flashlight's local BRONZE lake.

### Cost Explorer fallback

Grant `ce:GetCostAndUsage` to the billing scope that should be visible. For each requested
window, Flashlight calls `GetCostAndUsage` with daily granularity, the `UnblendedCost`
metric, and a `SERVICE` group-by. If `include_services` is configured, it becomes a Cost
Explorer service filter. The result is deliberately coarser than a FOCUS export: one daily
total per service, without resource, line-item, or charge-category detail.

### Optional AWS export-management commands

These commands are separate from ingest and should normally use a more privileged,
short-lived operator role:

| Command | AWS API calls | Required permission family | Why |
| --- | --- | --- | --- |
| `aws create-export` | `CreateExport` | `bcm-data-exports:CreateExport` | Creates a recurring FOCUS Data Export. |
| `aws update-export` | `ListExports`, `GetExport`, `UpdateExport` | Corresponding `bcm-data-exports` actions | Resolves the named export, then changes its destination or query settings. |
| `aws delete-export` | `ListExports`, `DeleteExport` | Corresponding `bcm-data-exports` actions | Resolves and deletes the export definition; it does not delete delivered S3 data. |
| `aws describe-export` | `ListExports`, `GetExport`, `s3:ListBucket` | Read-only Data Exports and S3 permissions | Shows export configuration and which delivered Parquet periods are present. |
| `aws bucket-policy` | `sts:GetCallerIdentity`, `s3:GetBucketPolicy`, `s3:PutBucketPolicy` | Read/write bucket-policy access | Merges the AWS Data Exports delivery statement into the bucket policy. |

AWS documents the available Data Exports IAM actions in its
[Data Exports access-control reference](https://docs.aws.amazon.com/cur/latest/userguide/bcm-data-exports-access.html).
Keep export management separate from the long-lived ingestion identity whenever possible.

## Validate

Run a one-month ingest, then compare the monthly effective-cost total with the same
period/scope in the AWS billing source. See [FOCUS and cost integrity](../concepts/focus-and-integrity.md)
for reconciliation rules.
