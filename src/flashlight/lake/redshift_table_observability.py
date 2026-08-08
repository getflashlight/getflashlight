"""Partition-replaced typed BRONZE Redshift table-observability facts."""

from __future__ import annotations

import shutil

from flashlight.core.settings import get_settings
from flashlight.lake import duck, paths
from flashlight.lake.redshift_table_observability_schema import (
    RedshiftTableObservabilityRecord,
    build_table,
)


def write(records: list[RedshiftTableObservabilityRecord]) -> int:
    """Replace only the cluster/day partitions represented by this pull.

    A missing historical system-log day must not erase older durable facts, so this
    intentionally purges the dates actually returned rather than an arbitrary ingest
    window. Daily scheduled pulls provide the authoritative history going forward.
    """
    if not records:
        return 0
    root = paths.redshift_table_observability_dir()
    for cluster_id, observation_date in {
        (record.cluster_id, record.observation_date) for record in records
    }:
        target = root / f"cluster_id={cluster_id}" / f"observation_date={observation_date:%Y-%m-%d}"
        if target.exists():
            shutil.rmtree(target)
    root.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    options = [
        "FORMAT parquet",
        "PARTITION_BY (cluster_id, observation_date)",
        f"COMPRESSION '{settings.parquet_compression}'",
        "APPEND",
    ]
    if settings.parquet_compression == "zstd":
        options.insert(3, f"COMPRESSION_LEVEL {settings.parquet_compression_level}")
    con = duck.connect()
    try:
        con.register("_rows", build_table(records))
        con.execute(f"COPY _rows TO '{root}' ({', '.join(options)})")  # noqa: S608
    finally:
        con.close()
    return len(records)
