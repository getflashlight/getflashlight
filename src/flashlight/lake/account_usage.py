"""ACCOUNT_USAGE Parquet writes: partition-replace per (provider, table, charge-month).

Visibility/LeaderBoard read this lake via :mod:`flashlight.dashboard.snowflake.visibility_data`
— never live Snowflake. Sibling of :func:`~flashlight.lake.paths.metrics_dir` so recursive
efficiency globs stay clean.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake import paths
from flashlight.lake.account_usage_schema import AccountUsageBatch

logger = get_logger(__name__)

_PROVIDER = "Snowflake"


def _window_months(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def _table_month_dir(provider: str, table: str, charge_month: str) -> Path:
    return (
        paths.account_usage_dir()
        / f"provider_name={provider}"
        / table
        / f"charge_month={charge_month}"
    )


def _purge_batches(batches: list[AccountUsageBatch], window: IngestWindow) -> None:
    """Remove partitions covered by this write (and empty months in the window for those tables)."""
    touched: set[tuple[str, str]] = {(b.provider_name, b.table_name) for b in batches}
    months = _window_months(window.start, window.end)
    for provider, table in touched:
        for month in months:
            partition = _table_month_dir(provider, table, month)
            if partition.exists():
                shutil.rmtree(partition)
                logger.info(
                    "account_usage_partition_purged",
                    provider=provider,
                    table=table,
                    charge_month=month,
                )


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns and coerce Decimal numerics to float64.

    Snowflake's connector returns UPPERCASE names and DECIMAL values; dashboard
    SQL/arithmetic expects lowercase columns and Python floats (``CREDIT_PRICE``).
    """
    from decimal import Decimal

    if frame.empty:
        return frame
    out = frame.copy()
    out.columns = [str(c).lower() for c in out.columns]
    for col in out.columns:
        sample = out[col].dropna().head(1)
        if sample.empty:
            continue
        value = sample.iloc[0]
        if isinstance(value, Decimal):
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def write_account_usage(window: IngestWindow, batches: list[AccountUsageBatch]) -> int:
    """Partition-replace ``[window]`` with ``batches``. Returns total rows written."""
    if not batches:
        logger.info("account_usage_window_empty")
        return 0

    _purge_batches(batches, window)
    paths.account_usage_dir().mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    compression = settings.parquet_compression
    rows = 0
    for batch in batches:
        frame = _normalize_frame(batch.frame)
        if frame.empty:
            continue
        dest = _table_month_dir(batch.provider_name, batch.table_name, batch.charge_month)
        dest.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pq.write_table(
            table,
            dest / "data.parquet",
            compression=compression if compression != "uncompressed" else None,
        )
        rows += len(frame)
    logger.info("account_usage_window_written", rows=rows, batches=len(batches))
    return rows


def install_flat_parquets(
    source_dir: Path,
    *,
    provider: str = _PROVIDER,
    charge_month: str | None = None,
) -> int:
    """Copy flat ``<table>.parquet`` files into the Hive lake layout (demo / sample).

    Each file becomes ``provider_name=…/<stem>/charge_month=…/data.parquet``. When
    ``charge_month`` is omitted, uses today's ``YYYY-MM``.
    """
    if charge_month is None:
        today = date.today()
        charge_month = f"{today.year:04d}-{today.month:02d}"
    paths.account_usage_dir().mkdir(parents=True, exist_ok=True)
    written = 0
    for parquet in sorted(source_dir.glob("*.parquet")):
        frame = pd.read_parquet(parquet)
        batch = AccountUsageBatch(
            provider_name=provider,
            table_name=parquet.stem.lower(),
            charge_month=charge_month,
            frame=frame,
        )
        # One-table purge for this month only
        dest = _table_month_dir(provider, batch.table_name, charge_month)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(_normalize_frame(frame), preserve_index=False)
        pq.write_table(table, dest / "data.parquet")
        written += len(frame)
        logger.info(
            "account_usage_demo_installed",
            table=batch.table_name,
            rows=len(frame),
            charge_month=charge_month,
        )
    return written


def clear_account_usage() -> None:
    """Remove the entire ACCOUNT_USAGE lake root (sample cleanup)."""
    root = paths.account_usage_dir()
    if root.exists():
        shutil.rmtree(root)
        logger.info("account_usage_cleared", path=str(root))


def has_parquet() -> bool:
    """Whether any ACCOUNT_USAGE Parquet exists under the lake root."""
    root = paths.account_usage_dir()
    if not root.exists():
        return False
    return any(root.rglob("*.parquet"))


def table_parquet_paths(table_name: str) -> list[Path]:
    """All Parquet files for a view stem (Hive partitions and flat lake files)."""
    root = paths.account_usage_dir()
    if not root.exists():
        return []
    stem = table_name.lower()
    hive = sorted(root.glob(f"provider_name=*/{stem}/charge_month=*/*.parquet"))
    if hive:
        return hive
    flat = root / f"{stem}.parquet"
    return [flat] if flat.exists() else []


def group_batches_by_month(
    provider: str,
    table_name: str,
    frame: pd.DataFrame,
    date_col: str,
    window: IngestWindow,
) -> list[AccountUsageBatch]:
    """Split a windowed frame into per-month batches using ``date_col``."""
    if frame.empty:
        return []
    normalized = _normalize_frame(frame)
    col = date_col.lower()
    if col not in normalized.columns:
        # Snapshot-style: stamp entire frame into each window month's partition once
        # is wrong for volume — use end month only.
        end_month = f"{window.end.year:04d}-{window.end.month:02d}"
        return [
            AccountUsageBatch(
                provider_name=provider,
                table_name=table_name,
                charge_month=end_month,
                frame=normalized,
            )
        ]
    ts = pd.to_datetime(normalized[col], errors="coerce")
    months = ts.dt.strftime("%Y-%m")
    batches: list[AccountUsageBatch] = []
    for month, group in normalized.groupby(months, dropna=True):
        if not isinstance(month, str) or month == "NaT":
            continue
        batches.append(
            AccountUsageBatch(
                provider_name=provider,
                table_name=table_name,
                charge_month=month,
                frame=group.reset_index(drop=True),
            )
        )
    return batches


def snapshot_batch(
    provider: str,
    table_name: str,
    frame: pd.DataFrame,
    window: IngestWindow,
) -> list[AccountUsageBatch]:
    """Point-in-time inventory: one partition at the window end month."""
    if frame.empty:
        return []
    end_month = f"{window.end.year:04d}-{window.end.month:02d}"
    return [
        AccountUsageBatch(
            provider_name=provider,
            table_name=table_name,
            charge_month=end_month,
            frame=_normalize_frame(frame),
        )
    ]
