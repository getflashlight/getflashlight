"""Client driver health — a fleet-health/compliance leaderboard, not waste.

Reads the one GOLD passthrough view (``driver_health.driver_health``): query volume
per (client_driver, client_application, executed_by, month). No dollar figure, and
still no verdict against the provider's *actual* released versions — there's no
reference table of those in this data.

What *is* computed here, presentation-only, is a self-referential staleness check:
``client_driver`` carries a family/version label (for example, ``DatabricksJDBCDriver,
2.7.1`` or ``Redshift JDBC Driver 2.0.0.0``), so within a family we can tell who's
behind the newest version already running *somewhere else in the same fleet* this
month — real drift, independent of whether that "newest seen" is itself the vendor's
latest release. That's the genuinely actionable read of this tab: not the raw
leaderboard, but who's lagging their own peers.
"""

from __future__ import annotations

import re

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df

_VERSION_SUFFIX = re.compile(r"^(?P<family>.+?)[, ]+(?P<version>\d+(?:\.\d+)+)$")

_COLS = [
    "client_driver",
    "executed_by",
    "provider_name",
    "query_count",
    "status_label",
]
_RENAME = {
    "client_driver": "Driver",
    "executed_by": "User",
    "provider_name": "Provider",
    "query_count": "Queries",
    "status_label": "Status",
}
_OUTDATED_COLS = [
    "client_driver",
    "executed_by",
    "newest_seen_version",
    "query_count",
]
_OUTDATED_RENAME = {
    "client_driver": "Driver",
    "executed_by": "User",
    "newest_seen_version": "Newest seen in fleet",
    "query_count": "Queries",
}

STATUS_BEHIND = "behind"
STATUS_CURRENT = "up_to_date"
STATUS_UNKNOWN = "unknown"


def _df(sql: str) -> pd.DataFrame:
    """Query the driver-health view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def _parse_driver(client_driver: object) -> tuple[str, str]:
    """Split a driver label into family/version when it has a numeric suffix.

    Databricks uses ``"DatabricksJDBCDriver, 2.7.1"`` while Redshift connection logs
    use labels such as ``"Redshift JDBC Driver 2.0.0.0"``.  This is presentation-only:
    it separates either source's family and version for a within-fleet comparison.
    Falls back to ``(raw, "")`` when there's no ", version" suffix, so an unexpected
    format degrades to "can't judge" rather than raising. ``client_driver`` is a
    nullable Parquet string column, which pandas surfaces as ``float('nan')`` (not
    ``None``) for a missing value — and ``nan`` is truthy, so this must check for a
    string explicitly rather than relying on falsiness.
    """
    if not isinstance(client_driver, str):
        return "", ""
    match = _VERSION_SUFFIX.match(client_driver.strip())
    if match is None:
        return client_driver.strip(), ""
    return match["family"].strip(), match["version"]


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric sort key for a dotted version string: "2.10.1" -> (2, 10, 1).

    A display heuristic, not a strict semver parser — a non-numeric or missing
    segment maps to -1 so an unparseable version never outranks one we can read,
    rather than raising.
    """
    if not version:
        return (-1,)
    return tuple(int(part) if part.isdigit() else -1 for part in version.split("."))


def _with_staleness(records: pd.DataFrame) -> pd.DataFrame:
    """Add ``driver_family``/``driver_version``/``newest_seen_version``/``status``.

    "Newest seen" is the max version of that (provider, family) across *all*
    history in ``records``, not just the displayed month — someone who upgraded
    last quarter still counts as the fleet's newest, even if they haven't queried
    since.
    """
    parsed = records["client_driver"].map(_parse_driver)
    records = records.assign(
        driver_family=[p[0] for p in parsed],
        driver_version=[p[1] for p in parsed],
    )

    version_cols = ["provider_name", "driver_family", "driver_version"]
    versions = records.loc[records["driver_version"] != "", version_cols].drop_duplicates()
    versions = versions.assign(_key=versions["driver_version"].map(_version_key))
    newest_idx = versions.groupby(["provider_name", "driver_family"])["_key"].idxmax()
    newest_by_family = versions.loc[newest_idx].set_index(["provider_name", "driver_family"])[
        "driver_version"
    ]

    keys = list(zip(records["provider_name"], records["driver_family"], strict=True))
    records["newest_seen_version"] = [newest_by_family.get(k, "") for k in keys]

    def _status(row: pd.Series) -> str:
        if not row["driver_version"] or not row["newest_seen_version"]:
            return STATUS_UNKNOWN
        if _version_key(row["driver_version"]) < _version_key(row["newest_seen_version"]):
            return STATUS_BEHIND
        return STATUS_CURRENT

    records["status"] = records.apply(_status, axis=1)
    records["status_label"] = records.apply(
        lambda r: {
            STATUS_BEHIND: f"Behind (newest seen {r['newest_seen_version']})",
            STATUS_CURRENT: "Up to date",
            STATUS_UNKNOWN: "—",
        }[r["status"]],
        axis=1,
    )
    return records


def render(provider_name: str = "Databricks", label: str = "Databricks") -> None:
    """Render one provider's driver fleet, never mixing version baselines.

    ``provider_name`` is the raw source name stored in the record (``AWS`` for
    Redshift), while *label* is the provider-page display name.
    """
    chrome.section_title(f"{label} client driver health")
    provider_sql = provider_name.replace("'", "''")

    records = _df(
        "SELECT * FROM driver_health.driver_health "
        f"WHERE provider_name = '{provider_sql}' "
        "ORDER BY charge_month DESC, query_count DESC"
    )
    if records.empty:
        ui.label(
            f"No driver-health data yet for {label}. Run `flashlight ingest` with its "
            "connector configured; this signal is collected independently from cost data."
        ).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    records = _with_staleness(records)

    months = sorted(records["charge_month"].astype(str).unique())
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]

    outdated = month_rows[month_rows["status"] == STATUS_BEHIND].sort_values(
        "query_count", ascending=False
    )
    with chrome.panel():
        chrome.panel_title("Outdated drivers")
        if outdated.empty:
            chrome.section_caption(
                "Every driver in use this month matches the newest version of its family "
                "already seen elsewhere in your fleet."
            )
        else:
            chrome.flat_table(
                outdated[_OUTDATED_COLS],
                key="driver_health_outdated",
                int_cols=["query_count"],
                rename=_OUTDATED_RENAME,
                pagination=10,
            )

    with chrome.panel():
        chrome.searchable_table(
            month_rows[_COLS],
            key="driver_health",
            search_col="client_driver",
            int_cols=["query_count"],
            rename=_RENAME,
            pagination=10,
        )
