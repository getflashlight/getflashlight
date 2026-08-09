"""Partition-replaced typed Bronze Redshift policy-configuration snapshots."""

from __future__ import annotations

import shutil

from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake import duck, paths
from flashlight.lake.redshift_policy_config_schema import RedshiftPolicyConfigRecord, build_table


def write(window: IngestWindow, records: list[RedshiftPolicyConfigRecord]) -> int:
    if not records:
        return 0
    root = paths.redshift_policy_config_dir()
    for record in records:
        target = (
            root
            / f"provider_name={record.provider_name}"
            / f"snapshot_month={record.snapshot_month:%Y-%m}"
        )
        if target.exists():
            shutil.rmtree(target)
    root.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    options = (
        "FORMAT parquet, PARTITION_BY (provider_name, snapshot_month), "
        f"COMPRESSION '{settings.parquet_compression}', APPEND"
    )
    con = duck.connect()
    try:
        con.register("_rows", build_table(records))
        con.execute(f"COPY _rows TO '{root}' ({options})")  # noqa: S608
    finally:
        con.close()
    return len(records)
