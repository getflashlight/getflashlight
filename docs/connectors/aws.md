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

## Required access

Credentials require access to the configured S3 export location, or `ce:GetCostAndUsage`
for Cost Explorer. The export-management commands additionally require the relevant AWS
Billing and Cost Management Data Exports permissions. Scope S3 and IAM access to the
specific bucket/prefix and account whenever possible.

## Validate

Run a one-month ingest, then compare the monthly effective-cost total with the same
period/scope in the AWS billing source. See [FOCUS and cost integrity](../concepts/focus-and-integrity.md)
for reconciliation rules.
