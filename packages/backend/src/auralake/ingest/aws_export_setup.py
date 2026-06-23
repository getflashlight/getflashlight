"""CLI to create the AWS FOCUS 1.2 Data Export that the aws_focus connector reads.

Auralake *consumes* a FOCUS export from S3; it does not create one. This command
wraps the ``bcm-data-exports:CreateExport`` API so customers can provision that
export reproducibly instead of click-ops in the console.

It is **dry-run by default** — CreateExport provisions a billing resource that
refreshes daily, so you must pass ``--apply`` to actually create it. Bucket /
prefix / region default from your ``connections.yml`` aws_focus block.

Prerequisite (one-time, not done here): the destination bucket policy must grant
``bcm-data-exports.amazonaws.com`` ``s3:PutObject`` — see the AWS Data Exports
S3 bucket setup docs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import yaml

from auralake.core.logging import get_logger
from auralake.core.settings import get_settings
from auralake.ingest.config import env

logger = get_logger(__name__)

# BCM Data Exports is a global (billing) service: its API endpoint is us-east-1
# regardless of where the destination S3 bucket lives.
_API_REGION = "us-east-1"

# SQL table name for "FOCUS 1.2 with AWS columns" (per the Data Exports table
# dictionary). FOCUS 1.0 was FOCUS_1_0_AWS; the table id encodes the version,
# which is why migrating versions means recreating the export.
FOCUS_TABLE = "FOCUS_1_2_AWS"

# Columns the FOCUS mapper consumes (mirror of _focus_map.map_focus_row). Selecting
# exactly these keeps the export aligned with what we ingest — and they are the
# same PascalCase names the connector already reads out of the Parquet, so the
# projection is self-consistent with the rest of the pipeline.
_FOCUS_COLUMNS: tuple[str, ...] = (
    "ProviderName", "BillingAccountId", "BillingAccountName", "SubAccountId",
    "SubAccountName", "BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart",
    "ChargePeriodEnd", "BillingCurrency", "BilledCost", "EffectiveCost", "ListCost",
    "ContractedCost", "ChargeCategory", "ChargeClass", "ChargeDescription",
    "ServiceCategory", "ServiceName", "SkuId", "RegionId", "ResourceId",
    "ResourceName", "ResourceType", "ConsumedQuantity", "ConsumedUnit", "Tags",
)


def default_query_statement() -> str:
    """The FOCUS projection Auralake ingests. Override with --query-statement."""
    return "SELECT " + ", ".join(_FOCUS_COLUMNS) + f" FROM {FOCUS_TABLE}"


def build_export_request(
    *,
    name: str,
    description: str,
    s3_bucket: str,
    s3_prefix: str,
    s3_region: str,
    query_statement: str,
    time_granularity: str,
    overwrite: str,
) -> dict[str, Any]:
    """Build the ``Export`` payload for bcm-data-exports:CreateExport (pure)."""
    return {
        "Name": name,
        "Description": description,
        "DataQuery": {
            "QueryStatement": query_statement,
            "TableConfigurations": {FOCUS_TABLE: {"TIME_GRANULARITY": time_granularity}},
        },
        "DestinationConfigurations": {
            "S3Destination": {
                "S3Bucket": s3_bucket,
                "S3Prefix": s3_prefix,
                "S3Region": s3_region,
                "S3OutputConfigurations": {
                    "OutputType": "CUSTOM",
                    "Format": "PARQUET",
                    "Compression": "PARQUET",
                    "Overwrite": overwrite,
                },
            }
        },
        "RefreshCadence": {"Frequency": "SYNCHRONOUS"},
    }


def _load_aws_focus_defaults(path: str | None) -> dict[str, Any]:
    """Read the aws_focus block from connections.yml (even if disabled) for defaults."""
    cfg_path = Path(path or get_settings().connections_path)
    if not cfg_path.exists():
        return {}
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    for entry in raw.get("connectors", []):
        if isinstance(entry, dict) and entry.get("type") == "aws_focus":
            return entry
    return {}


def perform_create_export(
    *,
    apply: bool,
    name: str,
    description: str,
    bucket: str | None,
    prefix: str | None,
    s3_region: str | None,
    time_granularity: str,
    overwrite: str,
    query_statement: str | None,
    connections: str | None,
) -> None:
    """Build (and optionally create) the FOCUS export. Backs ``auralake aws create-export``.

    Dry-run by default — prints the request. Pass ``apply=True`` to call CreateExport.
    Raises ``ValueError`` if no bucket can be resolved.
    """
    defaults = _load_aws_focus_defaults(connections)
    bucket = bucket or defaults.get("s3_bucket")
    prefix = prefix if prefix is not None else defaults.get("s3_prefix", "")
    s3_region = s3_region or defaults.get("region", "us-east-1")
    if not bucket:
        raise ValueError("no S3 bucket: pass --bucket or set s3_bucket in connections.yml")

    request = build_export_request(
        name=name,
        description=description,
        s3_bucket=bucket,
        s3_prefix=prefix or "",
        s3_region=s3_region,
        query_statement=query_statement or default_query_statement(),
        time_granularity=time_granularity,
        overwrite=overwrite,
    )

    if not apply:
        print(json.dumps({"Export": request}, indent=2))
        print("\nDRY RUN — no export created. Re-run with --apply to create it.")
        print(
            "Prerequisite: the destination bucket policy must grant "
            "bcm-data-exports.amazonaws.com the s3:PutObject action."
        )
        return

    client = boto3.client(
        "bcm-data-exports",
        region_name=_API_REGION,
        aws_access_key_id=env(defaults.get("access_key_env", "AWS_ACCESS_KEY_ID")),
        aws_secret_access_key=env(defaults.get("secret_key_env", "AWS_SECRET_ACCESS_KEY")),
    )
    resp = client.create_export(Export=request)
    arn = resp.get("ExportArn")
    logger.info("aws_export_created", arn=arn, bucket=bucket, prefix=prefix)
    print(f"Created export: {arn}")
    print("First data delivery can take up to 24 hours. Then run: auralake ingest")
