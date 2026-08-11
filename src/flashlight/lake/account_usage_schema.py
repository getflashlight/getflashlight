"""Typed batch for Snowflake ACCOUNT_USAGE table dumps.

Visibility/LeaderBoard SQL runs against these Parquet snapshots locally. Columns are
lowercased at write time so DuckDB SQL matches the synthetic demo shape.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class AccountUsageBatch:
    """One ACCOUNT_USAGE table for one provider × charge-month partition."""

    provider_name: str
    table_name: str  # lowercase stem, e.g. warehouse_metering_history
    charge_month: str  # YYYY-MM
    frame: pd.DataFrame
