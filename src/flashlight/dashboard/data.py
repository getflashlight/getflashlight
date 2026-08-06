"""Dashboard data access — GOLD Parquet → pandas.

Queries run through a throwaway in-memory DuckDB with ``<group>.<view>`` registered
(:func:`flashlight.lake.duck.register_gold`). No caching — DuckDB-over-Parquet reads
are already fast, and a new ``flashlight ingest`` publish should be visible on the
next query with no invalidation logic to get wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pandas as pd

from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.lake import duck, paths


def to_date(value: object) -> date:
    """Coerce a DuckDB/pandas date-ish scalar to a plain ``date``."""
    ts = pd.Timestamp(value)
    return date(ts.year, ts.month, ts.day)


NO_DATA_MSG = (
    "No billing data yet. Ask your admin to connect your cloud accounts in Flashlight."
)


def has_data() -> bool:
    """True once at least one GOLD view has been published."""
    return any(paths.gold_dir().glob("*/*.parquet"))


def gold_view_published(group: str, view: str) -> bool:
    """True when ``gold/<group>/<view>.parquet`` exists on disk.

    Same source of truth as :func:`flashlight.lake.duck.register_gold`, which only
    registers files that exist — so a view added to the catalog but absent from the
    last publish (a lake that hasn't been re-transformed since an upgrade) is simply
    not in the DuckDB catalog, and querying it raises. Panels reading a view newer
    than the lake check this first and skip instead of taking the page down.
    """
    return (paths.gold_dir() / group / f"{view}.parquet").exists()


def _aws_label() -> str:
    """``"AWS Redshift"`` while the group holds only Redshift's own services, plain
    ``"AWS"`` once it holds more.

    This is derived rather than a static string because the AWS pull is no longer
    Redshift-only *by definition*: ``AwsFocusConfig.include_services`` now defaults to
    Redshift + S3, since the storage behind Unity Catalog is billed by AWS and
    Databricks' own DBU-only bill can't show it (see docs/design/backing-storage.md). A
    fixed "AWS Redshift" would then be wrong for the group total on Home, and a fixed
    "AWS" was already wrong for a Redshift-only install — the honest label depends on
    what was actually ingested, which only the data knows.

    Fails toward the **narrower** label on any query problem: claiming less than the
    group holds is a smaller lie than implying the whole account is here.
    """
    try:
        df = gold_df('SELECT DISTINCT service_name FROM "aws".spend_by_service_month')
    except Exception:  # noqa: BLE001 - an unpublished/stale lake must not break the nav
        return "AWS Redshift"
    if df.empty:
        return "AWS Redshift"
    services = {str(s) for s in df["service_name"]}
    return "AWS Redshift" if services <= set(REDSHIFT_SERVICE_NAMES) else "AWS"


# Display-label resolvers, by provider group. Purely cosmetic — the group id and the
# `provider_name` in the data are untouched (see provider_name_for_group).
#
# Only `aws` needs one: "AWS" alone can overstate what the group holds, because
# `aws_focus` ingests a service-scoped slice of the AWS bill
# (AwsFocusConfig.include_services) rather than the whole account. A resolver rather
# than a constant keeps that judgement tied to the data — see _aws_label.
_GROUP_LABEL_RESOLVERS: dict[str, Callable[[], str]] = {"aws": _aws_label}


def provider_name_for_group(group: str) -> str:
    """The raw FOCUS ``provider_name`` in a group's data (e.g. ``"AWS"``).

    Distinct from :func:`provider_label` on purpose: this is the value to filter or
    join on, that one is what a human reads. Falls back to the titled slug when the
    group has no queryable rows.
    """
    try:
        df = gold_df(f'SELECT provider_name FROM "{group}".monthly_bill LIMIT 1')
        if not df.empty and df["provider_name"].iloc[0]:
            return str(df["provider_name"].iloc[0])
    except Exception:  # noqa: BLE001 - fall back to a readable slug on any query issue
        pass
    return group.replace("_", " ").title()


def provider_label(group: str) -> str:
    """Human label for a provider group — its ``provider_name``, unless the group has
    a resolver in :data:`_GROUP_LABEL_RESOLVERS`.

    Display only. Never use the result in a SQL predicate — see
    :func:`provider_name_for_group`.
    """
    resolver = _GROUP_LABEL_RESOLVERS.get(group)
    return resolver() if resolver else provider_name_for_group(group)


def gold_df(sql: str) -> pd.DataFrame:
    """Run *sql* over the GOLD views."""
    con = duck.connect()
    try:
        duck.register_gold(con)
        return con.execute(sql).df()
    finally:
        con.close()


def telemetry_df(sql: str) -> pd.DataFrame:
    """Run *sql* over the ``telemetry.assistant_turn`` view (BYOK assistant usage log)."""
    con = duck.connect()
    try:
        duck.register_assistant_turns(con)
        return con.execute(sql).df()
    finally:
        con.close()


def gold_last_updated() -> datetime | None:
    """Latest GOLD parquet mtime — proxy for when billing data was last published."""
    gold = paths.gold_dir()
    files = list(gold.glob("*/*.parquet"))
    if not files:
        return None
    ts = max(p.stat().st_mtime for p in files)
    return datetime.fromtimestamp(ts, tz=UTC)
