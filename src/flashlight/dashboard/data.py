"""Dashboard data access — GOLD Parquet → pandas.

Queries run through DuckDB with ``<group>.<view>`` registered
(:func:`flashlight.lake.duck.register_gold`). No cross-request caching — a new
``flashlight ingest`` publish should be visible on the next page render with no
invalidation logic to get wrong — but :func:`gold_session` scopes one registered
connection to a single page render, so its ~40-60 ``gold_df()`` calls share one
already-registered connection instead of each re-registering the whole lake from
scratch (see ``router.py``'s page handlers, which wrap their body in it).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime

import duckdb
import pandas as pd

from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.lake import duck, paths

# Set only inside a `with gold_session():` block (one per page render — see
# router.py). `None` outside one, which is what makes gold_df() fall back to its
# original open-register-close-per-call behavior for callers that render no page
# (scripts, tests, MCP-adjacent helpers). Deliberately a plain module-level
# ContextVar, not a global — contextvars propagate correctly through Starlette's
# per-request thread-pool dispatch of sync page handlers, so two concurrent page
# renders each see their own connection here, never each other's, with no lock
# shared across requests (unlike a process-wide cached connection would need).
_session_con: ContextVar[duckdb.DuckDBPyConnection | None] = ContextVar(
    "gold_session_con", default=None
)

# Dashboard panels often ask the same small question more than once while composing a
# page (for example, a date bound used by both a KPI and a chart).  GOLD is immutable for
# the life of a page render, so cache only within ``gold_session``: this avoids stale-data
# invalidation and keeps independent browser requests isolated.  DataFrames are mutable,
# therefore :func:`gold_df` returns a copy from this cache rather than exposing the cached
# object to a panel that might add display columns or sort it in place.
_session_results: ContextVar[dict[str, pd.DataFrame] | None] = ContextVar(
    "gold_session_results", default=None
)


@contextmanager
def gold_session() -> Iterator[None]:
    """Scope one registered DuckDB connection to everything run inside this block.

    Every ``gold_df()`` call inside the block transparently reuses it instead of
    opening a fresh connection and re-registering every GOLD file for every call —
    registration happens once per page render rather than once per panel query. A
    nested call (a helper that itself entered a session, or a page that renders
    another page's helpers) reuses the outer session's connection rather than
    opening — and closing — a second one out from under the outer block.
    """
    if _session_con.get() is not None:
        yield
        return
    con = duck.connect()
    duck.register_gold(con)
    connection_token = _session_con.set(con)
    results_token = _session_results.set({})
    try:
        yield
    finally:
        _session_results.reset(results_token)
        _session_con.reset(connection_token)
        con.close()


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

    Derived from ``aws.spend_by_service_month``, which excludes Amazon S3 and Amazon EC2
    (``silver.focus_provider_bill``) — both stay in bronze and surface as Databricks
    Storage / Databricks Compute via ``storage.backing_storage_month`` /
    ``compute.backing_compute_month``, so they must not widen this label.
    A non-Redshift service that *does* land in aws GOLD (e.g. a widened
    ``include_services``) still flips the label to plain ``"AWS"``.

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
    """Run *sql* over the GOLD views.

    Reuses the connection an enclosing :func:`gold_session` already registered, if
    there is one (the case for every dashboard page render — see ``router.py``).
    Falls back to opening, registering and closing its own connection when called
    with no session active, so this keeps working unchanged for callers outside a
    page render (scripts, tests, ad-hoc REPL use).
    """
    session_con = _session_con.get()
    if session_con is not None:
        results = _session_results.get()
        assert results is not None  # set alongside _session_con in gold_session()
        cached = results.get(sql)
        if cached is not None:
            return cached.copy(deep=True)
        result = session_con.execute(sql).df()
        results[sql] = result
        return result.copy(deep=True)
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
